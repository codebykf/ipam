"""IP Inventory — a small web app for tracking devices by country and subnet."""

import csv
import io
import ipaddress
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone

from flask import Flask, Response, g, jsonify, request, send_from_directory

DB_PATH = os.environ.get("DB_PATH", "/data/inventory.db")

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

SCHEMA = """
CREATE TABLE IF NOT EXISTS countries (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS subnets (
    id         INTEGER PRIMARY KEY,
    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    cidr       TEXT NOT NULL,
    gateway    TEXT NOT NULL,
    UNIQUE (country_id, cidr)
);
CREATE TABLE IF NOT EXISTS devices (
    id        INTEGER PRIMARY KEY,
    subnet_id INTEGER NOT NULL REFERENCES subnets(id) ON DELETE CASCADE,
    hostname  TEXT NOT NULL,
    ip        TEXT NOT NULL,
    notes     TEXT NOT NULL DEFAULT '',
    UNIQUE (subnet_id, ip)
);
CREATE TABLE IF NOT EXISTS changelog (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    summary    TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT ''
);
"""

HEADER_ALIASES = {
    "hostname": {"hostname", "host", "host name", "name", "device", "device name"},
    "ip": {"ip", "ip address", "ip_address", "ipaddress", "address", "ipv4", "ipv6"},
    "notes": {"notes", "note", "description", "comment", "comments", "info"},
}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify(error=str(err)), err.status


@app.errorhandler(413)
def handle_too_large(_err):
    return jsonify(error="File is too large (max 50 MB)"), 413


# --- Database -----------------------------------------------------------------

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --- Validation helpers -------------------------------------------------------

def require_text(data, key, label, max_len=255):
    value = str(data.get(key) or "").strip()
    if not value:
        raise ApiError(f"{label} is required")
    if len(value) > max_len:
        raise ApiError(f"{label} must be {max_len} characters or fewer")
    return value


def parse_network(value):
    try:
        return ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        raise ApiError(f"'{value}' is not a valid CIDR block (e.g. 10.0.0.0/24)")


def parse_ip_in(value, net, label="IP address"):
    value = str(value or "").strip()
    if not value:
        raise ApiError(f"{label} is required")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise ApiError(f"'{value}' is not a valid IP address")
    if ip not in net:
        raise ApiError(f"{label} {ip} is not inside {net}")
    return ip


def device_fields(data, net):
    hostname = require_text(data, "hostname", "Hostname")
    ip = parse_ip_in(data.get("ip"), net)
    notes = str(data.get("notes") or "").strip()
    if len(notes) > 5000:
        raise ApiError("Notes must be 5000 characters or fewer")
    return hostname, str(ip), notes


def subnet_fields(data):
    name = require_text(data, "name", "Subnet name")
    net = parse_network(require_text(data, "cidr", "CIDR block", 64))
    gateway = parse_ip_in(data.get("gateway"), net, "Gateway")
    return name, net, str(gateway)


def usable_hosts(net):
    if net.version == 4 and net.prefixlen < 31:
        return net.num_addresses - 2
    return net.num_addresses


def net_sort_key(cidr):
    net = ipaddress.ip_network(cidr)
    return (net.version, int(net.network_address), net.prefixlen)


def ip_sort_key(ip):
    addr = ipaddress.ip_address(ip)
    return (addr.version, int(addr))


def get_country(country_id):
    row = get_db().execute("SELECT * FROM countries WHERE id = ?", (country_id,)).fetchone()
    if row is None:
        raise ApiError("Country not found", 404)
    return row


def get_subnet(subnet_id):
    row = get_db().execute("SELECT * FROM subnets WHERE id = ?", (subnet_id,)).fetchone()
    if row is None:
        raise ApiError("Subnet not found", 404)
    return row


def subnet_dict(row, device_count=0):
    net = ipaddress.ip_network(row["cidr"])
    return {
        "id": row["id"],
        "country_id": row["country_id"],
        "name": row["name"],
        "cidr": row["cidr"],
        "gateway": row["gateway"],
        "usable_hosts": usable_hosts(net),
        "device_count": device_count,
    }


# --- Changelog ------------------------------------------------------------------

def log_change(db, action, entity, summary, details=""):
    """Record a change. Call before commit so the entry is part of the same transaction."""
    db.execute(
        "INSERT INTO changelog (created_at, action, entity, summary, details) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), action, entity, summary, details),
    )


def subnet_label(subnet):
    country = get_db().execute("SELECT name FROM countries WHERE id = ?", (subnet["country_id"],)).fetchone()
    return f"{country['name']} › {subnet['name']} ({subnet['cidr']})"


def describe_changes(old, new, labels):
    parts = []
    for key, label in labels.items():
        if str(old[key]) != str(new[key]):
            parts.append(f"{label} changed" if key == "notes" else f"{label}: {old[key]} → {new[key]}")
    return "; ".join(parts)


@app.get("/api/changelog")
def list_changelog():
    limit = min(request.args.get("limit", 200, type=int), 1000)
    before = request.args.get("before", type=int)
    sql, params = "SELECT * FROM changelog", []
    if before:
        sql, params = sql + " WHERE id < ?", [before]
    rows = get_db().execute(sql + " ORDER BY id DESC LIMIT ?", params + [limit + 1]).fetchall()
    return jsonify(entries=[dict(r) for r in rows[:limit]], more=len(rows) > limit)


# --- Pages ----------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# --- Countries ------------------------------------------------------------------

@app.get("/api/countries")
def list_countries():
    db = get_db()
    countries = db.execute("SELECT id, name FROM countries ORDER BY name COLLATE NOCASE").fetchall()
    subnets = db.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM devices d WHERE d.subnet_id = s.id) AS device_count "
        "FROM subnets s"
    ).fetchall()
    by_country = {}
    for s in subnets:
        by_country.setdefault(s["country_id"], []).append(subnet_dict(s, s["device_count"]))
    return jsonify([
        {
            "id": c["id"],
            "name": c["name"],
            "subnets": sorted(by_country.get(c["id"], []), key=lambda s: net_sort_key(s["cidr"])),
        }
        for c in countries
    ])


@app.post("/api/countries")
def create_country():
    name = require_text(request.get_json(silent=True) or {}, "name", "Country name", 100)
    db = get_db()
    try:
        cur = db.execute("INSERT INTO countries (name) VALUES (?)", (name,))
        log_change(db, "added", "country", f"Country {name}")
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError(f"Country '{name}' already exists", 409)
    return jsonify(id=cur.lastrowid, name=name), 201


@app.put("/api/countries/<int:country_id>")
def update_country(country_id):
    country = get_country(country_id)
    name = require_text(request.get_json(silent=True) or {}, "name", "Country name", 100)
    db = get_db()
    try:
        db.execute("UPDATE countries SET name = ? WHERE id = ?", (name, country_id))
        if name != country["name"]:
            log_change(db, "updated", "country", f"Country {country['name']} renamed to {name}")
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError(f"Country '{name}' already exists", 409)
    return jsonify(id=country_id, name=name)


@app.delete("/api/countries/<int:country_id>")
def delete_country(country_id):
    country = get_country(country_id)
    db = get_db()
    subnets = db.execute("SELECT name, cidr FROM subnets WHERE country_id = ?", (country_id,)).fetchall()
    devices = db.execute(
        "SELECT COUNT(*) FROM devices d JOIN subnets s ON s.id = d.subnet_id WHERE s.country_id = ?", (country_id,)
    ).fetchone()[0]
    db.execute("DELETE FROM countries WHERE id = ?", (country_id,))
    log_change(
        db, "deleted", "country", f"Country {country['name']} ({len(subnets)} subnet(s), {devices} device(s))",
        "\n".join(f"{s['name']} ({s['cidr']})" for s in subnets),
    )
    db.commit()
    return "", 204


# --- Subnets --------------------------------------------------------------------

@app.post("/api/countries/<int:country_id>/subnets")
def create_subnet(country_id):
    get_country(country_id)
    name, net, gateway = subnet_fields(request.get_json(silent=True) or {})
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO subnets (country_id, name, cidr, gateway) VALUES (?, ?, ?, ?)",
            (country_id, name, str(net), gateway),
        )
        log_change(db, "added", "subnet", f"Subnet {get_country(country_id)['name']} › {name} ({net}), gateway {gateway}")
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError(f"{net} already exists in this country", 409)
    return jsonify(subnet_dict(get_subnet(cur.lastrowid))), 201


@app.put("/api/subnets/<int:subnet_id>")
def update_subnet(subnet_id):
    subnet = get_subnet(subnet_id)
    name, net, gateway = subnet_fields(request.get_json(silent=True) or {})
    db = get_db()
    outside = [
        r["ip"] for r in db.execute("SELECT ip FROM devices WHERE subnet_id = ?", (subnet_id,))
        if ipaddress.ip_address(r["ip"]) not in net
    ]
    if outside:
        sample = ", ".join(outside[:3]) + ("…" if len(outside) > 3 else "")
        raise ApiError(f"{len(outside)} device(s) would fall outside {net}: {sample}")
    try:
        db.execute(
            "UPDATE subnets SET name = ?, cidr = ?, gateway = ? WHERE id = ?",
            (name, str(net), gateway, subnet_id),
        )
        diff = describe_changes(
            subnet, {"name": name, "cidr": str(net), "gateway": gateway},
            {"name": "name", "cidr": "CIDR", "gateway": "gateway"},
        )
        if diff:
            log_change(db, "updated", "subnet", f"Subnet {subnet_label(subnet)}: {diff}")
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError(f"{net} already exists in this country", 409)
    return jsonify(subnet_dict(get_subnet(subnet["id"])))


@app.delete("/api/subnets/<int:subnet_id>")
def delete_subnet(subnet_id):
    subnet = get_subnet(subnet_id)
    db = get_db()
    label = subnet_label(subnet)
    devices = db.execute("SELECT hostname, ip FROM devices WHERE subnet_id = ?", (subnet_id,)).fetchall()
    db.execute("DELETE FROM subnets WHERE id = ?", (subnet_id,))
    log_change(
        db, "deleted", "subnet", f"Subnet {label} ({len(devices)} device(s))",
        "\n".join(f"{d['hostname']} ({d['ip']})" for d in sorted(devices, key=lambda d: ip_sort_key(d["ip"]))),
    )
    db.commit()
    return "", 204


# --- Devices --------------------------------------------------------------------

def device_dict(row):
    return {"id": row["id"], "hostname": row["hostname"], "ip": row["ip"], "notes": row["notes"]}


def next_free_ip(net, used):
    for host in net.hosts():
        if str(host) not in used:
            return str(host)
    return None


@app.get("/api/subnets/<int:subnet_id>/devices")
def list_devices(subnet_id):
    subnet = get_subnet(subnet_id)
    rows = get_db().execute("SELECT * FROM devices WHERE subnet_id = ?", (subnet_id,)).fetchall()
    devices = sorted((device_dict(r) for r in rows), key=lambda d: ip_sort_key(d["ip"]))
    used = {d["ip"] for d in devices} | {subnet["gateway"]}
    return jsonify(devices=devices, next_ip=next_free_ip(ipaddress.ip_network(subnet["cidr"]), used))


@app.post("/api/subnets/<int:subnet_id>/devices")
def create_device(subnet_id):
    subnet = get_subnet(subnet_id)
    hostname, ip, notes = device_fields(request.get_json(silent=True) or {}, ipaddress.ip_network(subnet["cidr"]))
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO devices (subnet_id, hostname, ip, notes) VALUES (?, ?, ?, ?)",
            (subnet_id, hostname, ip, notes),
        )
        log_change(db, "added", "device", f"Device {hostname} ({ip}) in {subnet_label(subnet)}")
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError(f"{ip} is already assigned in this subnet", 409)
    return jsonify(id=cur.lastrowid, hostname=hostname, ip=ip, notes=notes), 201


@app.put("/api/devices/<int:device_id>")
def update_device(device_id):
    db = get_db()
    row = db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        raise ApiError("Device not found", 404)
    subnet = get_subnet(row["subnet_id"])
    hostname, ip, notes = device_fields(request.get_json(silent=True) or {}, ipaddress.ip_network(subnet["cidr"]))
    try:
        db.execute(
            "UPDATE devices SET hostname = ?, ip = ?, notes = ? WHERE id = ?",
            (hostname, ip, notes, device_id),
        )
        diff = describe_changes(
            row, {"hostname": hostname, "ip": ip, "notes": notes},
            {"hostname": "hostname", "ip": "IP", "notes": "notes"},
        )
        if diff:
            log_change(db, "updated", "device", f"Device {row['hostname']} ({row['ip']}) in {subnet_label(subnet)}: {diff}")
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError(f"{ip} is already assigned in this subnet", 409)
    return jsonify(id=device_id, hostname=hostname, ip=ip, notes=notes)


@app.delete("/api/devices/<int:device_id>")
def delete_device(device_id):
    db = get_db()
    row = db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        raise ApiError("Device not found", 404)
    label = subnet_label(get_subnet(row["subnet_id"]))
    db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    log_change(db, "deleted", "device", f"Device {row['hostname']} ({row['ip']}) from {label}")
    db.commit()
    return "", 204


@app.get("/api/search")
def search_devices():
    """Find devices by hostname or IP across every subnet, for the header search box."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    pattern = "%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"
    rows = get_db().execute(
        "SELECT d.id, d.hostname, d.ip, d.subnet_id, s.name AS subnet_name, s.cidr, c.name AS country_name "
        "FROM devices d JOIN subnets s ON s.id = d.subnet_id JOIN countries c ON c.id = s.country_id "
        "WHERE d.hostname LIKE ? ESCAPE '\\' OR d.ip LIKE ? ESCAPE '\\' "
        "ORDER BY (d.ip = ? OR d.hostname = ? COLLATE NOCASE) DESC, d.hostname COLLATE NOCASE "
        "LIMIT 20",
        (pattern, pattern, q, q),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# --- CSV import / export -------------------------------------------------------------

def read_csv_rows(upload):
    raw = upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        return list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ApiError(f"Could not read CSV: {exc}")


def detect_columns(first_row):
    """Return (column map, has_header). Without a header, columns are hostname, ip, notes."""
    header = [c.strip().lower() for c in first_row]
    found = {}
    for key, aliases in HEADER_ALIASES.items():
        for i, cell in enumerate(header):
            if cell in aliases and i not in found.values():
                found[key] = i
                break
    if not found:
        return {"hostname": 0, "ip": 1, "notes": 2}, False
    if "hostname" not in found or "ip" not in found:
        raise ApiError("CSV header must include 'hostname' and 'ip' columns")
    return found, True


@app.post("/api/subnets/<int:subnet_id>/import")
def import_devices(subnet_id):
    subnet = get_subnet(subnet_id)
    net = ipaddress.ip_network(subnet["cidr"])
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ApiError("Choose a CSV file to upload")
    update_existing = request.form.get("update") in ("on", "1", "true")

    rows = read_csv_rows(upload)
    non_blank = [(n, r) for n, r in enumerate(rows, start=1) if any(c.strip() for c in r)]
    if not non_blank:
        raise ApiError("The CSV file is empty")

    columns, has_header = detect_columns(non_blank[0][1])
    if has_header:
        non_blank = non_blank[1:]

    db = get_db()
    existing = {r["ip"] for r in db.execute("SELECT ip FROM devices WHERE subnet_id = ?", (subnet_id,))}
    seen, inserts, updates, errors = set(), [], [], []

    def cell(row, key):
        i = columns.get(key)
        return row[i] if i is not None and i < len(row) else ""

    for line, row in non_blank:
        try:
            hostname, ip, notes = device_fields(
                {"hostname": cell(row, "hostname"), "ip": cell(row, "ip"), "notes": cell(row, "notes")}, net
            )
        except ApiError as exc:
            errors.append({"line": line, "message": str(exc)})
            continue
        if ip in seen:
            errors.append({"line": line, "message": f"{ip} appears more than once in the file"})
        elif ip in existing:
            if update_existing:
                updates.append((hostname, notes, subnet_id, ip))
            else:
                errors.append({"line": line, "message": f"{ip} already exists in this subnet"})
        else:
            inserts.append((subnet_id, hostname, ip, notes))
        seen.add(ip)

    db.executemany("INSERT INTO devices (subnet_id, hostname, ip, notes) VALUES (?, ?, ?, ?)", inserts)
    db.executemany("UPDATE devices SET hostname = ?, notes = ? WHERE subnet_id = ? AND ip = ?", updates)
    if inserts or updates:
        counts = [f"{len(inserts)} device(s) added"] + ([f"{len(updates)} updated"] if updates else [])
        lines = [f"Added {h} ({ip})" for _, h, ip, _ in inserts] + [f"Updated {h} ({ip})" for h, _, _, ip in updates]
        log_change(
            db, "imported", "device",
            f"CSV import into {subnet_label(subnet)}: {', '.join(counts)}", "\n".join(lines),
        )
    db.commit()
    return jsonify(imported=len(inserts), updated=len(updates), skipped=len(errors), errors=errors[:200])


def csv_response(rows, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["hostname", "ip", "notes"])
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/subnets/<int:subnet_id>/export")
def export_devices(subnet_id):
    subnet = get_subnet(subnet_id)
    rows = get_db().execute("SELECT hostname, ip, notes FROM devices WHERE subnet_id = ?", (subnet_id,)).fetchall()
    rows = sorted(rows, key=lambda r: ip_sort_key(r["ip"]))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{subnet['name']}-{subnet['cidr']}").strip("-")
    return csv_response([(r["hostname"], r["ip"], r["notes"]) for r in rows], f"{slug}.csv")


@app.get("/api/csv-template")
def csv_template():
    return csv_response(
        [("core-switch-01", "10.0.0.2", "Rack A, U12"), ("printer-2f", "10.0.0.50", "")],
        "devices-template.csv",
    )


# --- Backup & restore -----------------------------------------------------------

SQLITE_HEADER = b"SQLite format 3\x00"

# Tables in insert order (parents first) and the columns copied on restore.
BACKUP_TABLES = {
    "countries": ["id", "name"],
    "subnets": ["id", "country_id", "name", "cidr", "gateway"],
    "devices": ["id", "subnet_id", "hostname", "ip", "notes"],
}


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def snapshot_to(path):
    """Write a consistent copy of the live database to path, safe while the app is running."""
    dest = sqlite3.connect(path)
    try:
        get_db().backup(dest)
        dest.execute("PRAGMA journal_mode = DELETE")  # single self-contained file, no -wal/-shm
    finally:
        dest.close()
    if os.path.exists(path + "-shm"):
        os.remove(path + "-shm")


def check_backup_file(path):
    with open(path, "rb") as f:
        if f.read(16) != SQLITE_HEADER:
            raise ApiError("That file isn't an IP Inventory backup (not a SQLite database)")
    try:
        src = sqlite3.connect(path)
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ApiError("The backup file is corrupted")
            for table, cols in BACKUP_TABLES.items():
                have = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
                if not have:
                    raise ApiError(f"That file isn't an IP Inventory backup (no '{table}' table)")
                missing = [c for c in cols if c not in have]
                if missing:
                    raise ApiError(f"Backup table '{table}' is missing column(s): {', '.join(missing)}")
        finally:
            src.close()
    except sqlite3.DatabaseError:
        raise ApiError("Could not read the backup file")


@app.get("/api/backup")
def backup_db():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "backup.db")
        snapshot_to(path)
        with open(path, "rb") as f:
            data = f.read()
    return Response(
        data,
        mimetype="application/vnd.sqlite3",
        headers={"Content-Disposition": f'attachment; filename="ip-inventory-{timestamp()}.db"'},
    )


@app.post("/api/restore")
def restore_db():
    """Replace all data with the contents of an uploaded backup, in a single transaction."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ApiError("Choose a backup file to upload")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "restore.db")
        upload.save(path)
        check_backup_file(path)

        # Keep a copy of the current data in case the restore was a mistake.
        safety_dir = os.path.join(os.path.dirname(DB_PATH) or ".", "backups")
        os.makedirs(safety_dir, exist_ok=True)
        snapshot_to(os.path.join(safety_dir, f"pre-restore-{timestamp()}.db"))

        conn = connect()
        conn.isolation_level = None  # manage the transaction explicitly
        try:
            conn.execute("ATTACH DATABASE ? AS src", (path,))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in reversed(list(BACKUP_TABLES)):
                    conn.execute(f"DELETE FROM {table}")
                for table, cols in BACKUP_TABLES.items():
                    col_list = ", ".join(cols)
                    conn.execute(f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM src.{table}")
                log_change(conn, "restored", "backup", f"Restored all data from backup file {upload.filename}")
                conn.execute("COMMIT")
            except sqlite3.DatabaseError as exc:
                conn.execute("ROLLBACK")
                raise ApiError(f"Backup contains invalid data, nothing was changed ({exc})")
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in BACKUP_TABLES}
            conn.execute("DETACH DATABASE src")
        finally:
            conn.close()
    return jsonify(counts)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

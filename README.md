# IP Inventory

A small self-hosted web app for recording devices by **location → subnet → device**.

## Features

- **Locations**: add them with the **+** button in the sidebar (for example a country, site or office). Click a location to expand its subnets.
- **Subnets**: add them to a location with a name, CIDR block and gateway. The gateway must sit inside the CIDR block. A subnet's page shows its CIDR, gateway, and how many of its usable addresses are taken.
- **Devices**: add them to a subnet with a hostname, IP address and notes. The IP must sit inside the subnet and can only be used once per subnet. **Add device** suggests the next free IP.
- **Search in the header**: type part of a device name or IP to search every location and subnet. Results show where each device lives (`Location › Subnet · CIDR`). Clicking one, or pressing Enter, opens its subnet and briefly highlights the device. Use the arrow keys to move through results, Escape to close them, and press `/` anywhere to jump to the search box.
- **Search devices**: on a subnet's page, filter its devices by hostname, IP or notes.
- **CSV import and export**: import many devices into a subnet from a CSV, or export a subnet's devices to CSV (see [CSV import](#csv-import)).
- **Changelog**: click **Changelog** at the bottom of the sidebar to see every location, subnet and device that was added, changed or deleted, plus CSV imports and restores, grouped by day. It shows 200 entries at a time, with **Load older changes** at the bottom.
- **Backup and restore**: from the bottom of the sidebar (see [Data & backups](#data--backups)).

## Run

```bash
docker compose up -d --build
```

Open http://localhost:8080. To use a different port, change `8080` in `docker-compose.yml`.

There is no login, and the port is published on all of the Docker host's network interfaces, so anyone who can reach port 8080 on that machine can view and edit the data. To keep it to the Docker host only, change the port line in `docker-compose.yml` to `"127.0.0.1:8080:8000"`.

After changing any file in the app (including the logo), run `docker compose up -d --build` again, because the image copies the app in when it's built.

## Data & backups

All data lives in a single SQLite database file, `./data/inventory.db`, on the host (bind-mounted into the container). Rebuilding or removing the container does not affect it.

Don't copy `inventory.db` while the app is running, because recent changes may still be in the `-wal` file. Back it up in one of these ways:

- **From the UI:** click **Download backup** at the bottom of the sidebar. This saves `ip-inventory-YYYYMMDD-HHMMSS.db`.
- **From a script / cron:** this gives a consistent snapshot without stopping the app:
  ```bash
  curl -fsS -o "backup-$(date +%F).db" http://localhost:8080/api/backup
  ```
  Example nightly crontab entry:
  ```
  0 2 * * * curl -fsS -o /path/to/backups/ip-inventory-$(date +\%F).db http://localhost:8080/api/backup
  ```
- **Offline:** run `docker compose stop`, copy the whole `data/` folder, then run `docker compose start`.

### Restore a backup

**From the UI:** click **Restore backup** at the bottom of the sidebar, or **Restore from backup** on the welcome screen of a fresh install, and upload a `.db` file.

- Restoring **replaces all current data**. If there is existing data, you must tick a confirmation box first.
- A copy of the data being replaced is saved to `data/backups/pre-restore-<timestamp>.db`, so a mistaken restore can be undone by restoring that file. These copies aren't deleted automatically.
- The file is validated first: it must be a SQLite database with the IP Inventory tables. The restore runs as a single transaction, so if anything is wrong, nothing is changed.
- Uploads are limited to 50 MB.

**Manually**, with the app stopped:

```bash
docker compose down
rm -f data/inventory.db-wal data/inventory.db-shm
cp /path/to/ip-inventory-YYYYMMDD-HHMMSS.db data/inventory.db
docker compose up -d
```

Backup files are standard SQLite databases, so you can open them with any SQLite tool (e.g. DB Browser for SQLite).

## CSV import

Open a subnet and click **Import CSV**. Columns are `hostname, ip, notes`. The header row and the notes column are both optional:

```csv
hostname,ip,notes
core-switch-01,10.0.0.2,"Rack A, U12"
printer-2f,10.0.0.50,
```

- Rows with an invalid IP, an IP outside the subnet, or a duplicate IP are skipped and listed by line number.
- Tick **Update hostname & notes when an IP already exists** to overwrite existing entries instead of skipping them.
- `sample-devices.csv` is an example file, and the import dialog also has a template download.
- **Export** on a subnet's page downloads its devices in the same format.

## Logo

The header shows `app/static/logo.svg` on the left, followed by a white divider and "IP INVENTORY". The file that ships is a dashed "YOUR LOGO" placeholder.

To use your own logo, replace `app/static/logo.svg` with a white or transparent-background version (the header is red) and rebuild. Then match its size:

- In `app/static/index.html`, set the `width` and `height` on the logo `<img>` to your file's dimensions.
- In `app/static/style.css`, `.logo` sets the displayed height (36px for the placeholder). A wide wordmark usually looks best at about 28px.

To use a PNG instead, change the `src` in `app/static/index.html`.

## Note for developers

Locations used to be called countries, and the code still uses that name internally: the `countries` database table, the `/api/countries` endpoints and the `country` changelog entity. Only the wording people see changed, so existing databases and backups work without any migration.

## Run without Docker (development)

```bash
pip install -r requirements.txt
cd app && DB_PATH=../data/inventory.db python app.py
```

This runs Flask's development server at http://localhost:8000. In Docker, the app runs under gunicorn instead.

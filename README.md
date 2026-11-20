# Immich filename-date fixer

A small web app that fixes the capture dates of [Immich](https://immich.app) photos
and videos by reading the date from their **filenames**, and lets you approve each
change by eye.

> **Status:** a personal utility, shared as-is. It is not a maintained product, so
> expect no support or roadmap. Read [Known limitations](#known-limitations) before
> approving thousands of changes.

## Why it exists

Photos from old phones, messaging apps, screenshots and scans often have no usable
date in their metadata, so Immich files them under the day they were imported. Many
of those files carry the real date in their name (`IMG_20190314_153022.jpg`,
`2012-04-25 07.40.png`). This tool finds them, proposes the date from the name, shows
you the photo next to the old and new dates, and writes the date back to Immich only
when you press **Approve**.

## How it works

1. **Connect.** Enter your Immich URL, an API key and your timezone in the page, then
   use **Test connectivity** to check each step.
2. **Process.** **Start processing dates** pages through every photo you own, parses a
   date out of each filename, and saves the proposals in a local SQLite file. This can
   take a while on a large library and keeps running if you close the tab.
3. **Review.** Proposals are shown one at a time with a preview, the date Immich shows
   now, and the proposed date. The proposed date is an editable field for one-off
   corrections.
   - **Approve** writes the date to Immich (`PUT /api/assets/{id}`) and records it as done.
   - **Skip** moves on and records nothing. Skipped items return after a page reload.
4. **Track.** The stats bar shows how many have been found, approved and are left.
   Re-running processing is safe: approved items are never touched, and pending ones
   are refreshed against Immich's current state.

Keyboard: `A` or `Enter` approves, `S` skips.

## Quick start

### Docker Compose

```bash
docker compose up -d --build
```

Then open <http://localhost:5055>. Settings and progress are kept in `./data`.

To use the prebuilt image instead of building (see [CI](#continuous-integration)),
edit `docker-compose.yml`: remove `build: .` and uncomment the `image:` line.

### Docker

```bash
docker run -d --name immich-dates \
  -p 127.0.0.1:5055:5055 \
  -v "$PWD/data:/data" \
  ghcr.io/OWNER/REPO:edge
```

### Without Docker

Needs Python 3.9 or newer.

```bash
pip install -r requirements.txt
python immich_dates.py        # http://127.0.0.1:5055
```

Without Docker, data is saved in the current directory unless you set `DATA_DIR`.

## Configuration

Immich details are entered in the web page, not in environment variables. Only the
server itself is configured by environment:

| Variable   | Default     | Meaning                                                              |
|------------|-------------|----------------------------------------------------------------------|
| `PORT`     | `5055`      | Port the app listens on (and, in Compose, the port published).      |
| `BIND`     | `127.0.0.1` | Compose only: host address the port is published on.                |
| `DATA_DIR` | `.` (`/data` in Docker) | Folder holding the SQLite files.                          |

`docker-compose.yml` reads `PORT` and `BIND` from the shell or from a `.env` file
(see `.env.example`).

### Getting an Immich API key

In Immich: **Account settings, API keys, New API key.** Grant these permissions (or
choose all):

- `user.read` for the connectivity test
- `asset.read` to scan your library
- `asset.update` to change dates
- `asset.view` to show thumbnails

### Reaching Immich from a container

Inside a container, `localhost` is the container itself. Use your server's LAN
address (for example `http://192.168.1.10:2283`) or, if both containers share a
Docker network, the Immich container's name.

### Troubleshooting

- **`unable to open database file`:** the data folder is not writable by the app. Current
  images fix this themselves at start-up, so rebuild or pull the latest image. On an
  older image, run `sudo chown 10001 ./data`.

## Multiple users

Each browser gets a random ID in a cookie (`uid`) and its own private profile: its own
Immich URL, API key, timezone, proposals and progress, stored as `<id>.db` in the data
folder. Several people can use one instance with their own Immich accounts.

- The cookie is the only credential. Anyone holding it can use that profile.
- Clearing cookies or changing browser or device starts a fresh profile. The old data
  stays on disk, unreachable.
- If a single-user `immich_dates.db` from an earlier version is in the data folder,
  the first browser to open the app adopts it.

## How dates are read from filenames

The first `YYYY MM DD` found in the name is used, with optional time. Year, month and
day must use the same separator (or none), so IDs such as `1975-1104` are not read as
dates.

| Filename                                   | Result               |
|--------------------------------------------|----------------------|
| `IMG_20190314_153022.jpg`                  | 2019-03-14 15:30:22  |
| `2019-03-14 15.30.22.jpg`                  | 2019-03-14 15:30:22  |
| `2012-04-25 07.40.png` (no seconds)        | 2012-04-25 07:40:00  |
| `Screenshot_2020-02-29-10-15-00.png`       | 2020-02-29 10:15:00  |
| `IMG-20190314-WA0001.jpg` (no time)        | 2019-03-14 12:00:00  |
| `171e1975-1104-40d2-b06a-3b5d36fb32ff.jpg` | no date (UUID)       |
| `20190230_101500.jpg` (30 Feb)             | no date (invalid)    |

- A date with no time gets **12:00:00**, so a timezone shift cannot move it to another day.
- Dates in the future are ignored.
- The proposed time is treated as being in the timezone you configured.

## What gets listed

- Only photos and videos **you own**. Photos shared with you by a partner are read-only
  for your key, so they are skipped (the stats line shows how many).
- By default only photos with **no** metadata date. Tick the checkbox in the connection
  panel to also list photos whose metadata date falls on a different day from the
  filename. Use this with care: it can overwrite a genuine camera date.

## Known limitations

- **Milliseconds in names lose the time.** `PXL_20260509_232300438.jpg` is proposed as
  12:00:00 instead of 23:23:00, because the extra digits after the time are not
  understood. Edit the date before approving, or skip these.
- **Pixel filenames are in UTC**, but are treated as your local time, so they can be
  an hour out in summer.
- **Messaging and download apps** (WhatsApp `IMG-…-WA…`, Instagram, Reddit, Twitter)
  put the date a file was received or downloaded in the name, not when it was taken,
  and have no time.
- **Time zones can make a mismatch look bigger than it is.** Immich may show UTC while
  the filename is local, so a photo can be listed when it is only an hour out.
- **Duplicates.** The same filename on several assets produces several proposals.
- **Trashed photos** are not scanned. Archived photos may not be, depending on your
  Immich version.
- The Immich API changes between versions. The app reads `originalFileName`,
  `localDateTime`, `ownerId` and `exifInfo.dateTimeOriginal` from
  `POST /api/search/metadata`. If a scan fails, check those fields first.

## Security

- **There is no login.** Whoever can reach the port can use the app.
- **API keys are stored in plain text** in the data folder. Keep it private.
- The browser never receives the API key back, and thumbnails are fetched through the
  app. Compose publishes the port on `127.0.0.1` only. If you widen it (`BIND=0.0.0.0`),
  do it on a trusted network or behind a reverse proxy that adds authentication.

## Resetting

Stop the app and delete the data folder (or one `<id>.db` file) to remove saved
settings and progress. This does not undo dates already written to Immich.

## Continuous integration

`.github/workflows/docker.yml` builds the image on every push to `main` and publishes
it to the GitHub Container Registry as `ghcr.io/<owner>/<repo>:edge`, for both
`linux/amd64` and `linux/arm64`.

- No secrets are needed. It signs in with the built-in `GITHUB_TOKEN`.
- It can also be started by hand from the repository's **Actions** tab.
- Pushes that change only `.md` files do not trigger a build.
- After the first run, open the package under the repository's **Packages** and set it
  to public if you want to pull it without logging in. A private package needs
  `docker login ghcr.io` first.
- `edge` always points at the latest `main`. There are no versioned tags.

## Files

| File                           | Purpose                                       |
|--------------------------------|-----------------------------------------------|
| `immich_dates.py`              | The whole app: scanner, API and web page.     |
| `requirements.txt`             | Python dependencies.                          |
| `Dockerfile`                   | Alpine-based image served by gunicorn.        |
| `docker-compose.yml`           | Compose setup with port and data volume.      |
| `.env.example`                 | Optional `PORT` and `BIND` settings.          |
| `.dockerignore`                | Keeps local databases and caches out of builds. |
| `.github/workflows/docker.yml` | Image build and publish.                      |
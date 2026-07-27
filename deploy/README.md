# Running TaskHome as a service

TaskHome is an appliance: it should start at boot, survive a crash, and keep
printing without anyone logged in.

```sh
./deploy/install.sh            # detects the platform, asks before changing anything
./deploy/install.sh --dry-run  # show the steps without running them
./deploy/install.sh --uninstall
./deploy/healthcheck.sh        # exits 0 when healthy
```

The units ship with `CHANGE_ME` placeholders and `install.sh` refuses to run
until they are filled in — installing a unit that points at the wrong user is
worse than not installing one.

| File | Platform | Purpose |
| --- | --- | --- |
| `taskhome.service` | Linux | systemd **system** unit |
| `99-taskhome-printer.rules` | Linux | non-root USB access to the printer |
| `com.micatechnologies.taskhome.plist` | macOS | launchd agent |
| `healthcheck.sh` | both | is it actually working? |
| `install.sh` | both | install / uninstall |

## The udev rule is not optional on Linux

Without it, libusb can only claim the printer as root, so the service reports
"Printer not connected" no matter what — with no other symptom. The rule also
unbinds the `usblp` kernel driver, which otherwise holds the device and makes
libusb fail with "Resource busy".

Install it, then **replug the printer** — udev rules apply at device
attachment, not retroactively. The service user must also be in `plugdev`.

## Why a system service, not a user service

The busylight daemon in dev-configurations is a user service, which is right
for something tied to a desktop session. TaskHome is the opposite: it must run
headless from boot and keep running after logout.

`Restart=always`, not `on-failure` — a clean exit is still an appliance that
has stopped printing, which is the failure that matters.

`ExecStart` points at `scripts/run.sh` rather than at Python directly, so a
host that loses its interpreter repairs the virtualenv on the next restart
instead of failing forever.

## Hardening note

`PrivateDevices=yes` is deliberately **not** set: it hides `/dev/bus/usb` and
the printer vanishes. `ProtectSystem=full` and `ReadWritePaths` are set instead.

## What healthcheck.sh checks

More than "the port answers". A process serving pages while its scheduler
thread has died is exactly the failure that went unnoticed for a week before
`P0-1` was found, so it also checks that each store parses — a store that
failed to load is write-blocked, which is invisible from outside — and that
the log has been written recently.

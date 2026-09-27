# Changelog

Notable changes to the firmware, web interface, and device behaviour.
The development history before the first public release is not published.

## V15.8.1 – 2026-09-24

- Keep total points, the active point limit and remaining capacity visible on
  the status screen, with warnings at 80, 90, 98 and 100 percent.
- Estimate time to the point limit from recent recorded points; suppress the
  estimate during pauses, stale data and insufficient observation.
- Select another project to archive directly from the capacity display, showing
  how many points it frees. Preserve the paused line for explicit resumption.
- Allow archiving and deleting other projects while a line remains active;
  continue protecting the project that owns that line.
- Distinguish the point limit from the device's free storage and reserve.

## V15.8.0 – 2026-09-22

First public release.

- Product name ESP-RTK; the factory fallback network is ESP-RTK-Setup.
- English documentation: README, technical reference, onboarding,
  integration contract, security model, performance limits, and known
  limitations.
- See docs/known-limitations.md for the weaknesses that are documented
  rather than fixed in this release.

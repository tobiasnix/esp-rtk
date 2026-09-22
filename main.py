# SPDX-License-Identifier: AGPL-3.0-only
"""Entry point: watchdog, GC, supervision and task start. MicroPython runs this file at launch. It deliberately holds little logic of its own - it lies in cfg, state, net, web, ble and gnss.
"""
import gc
import machine
import micropython
import time
import uasyncio as asyncio
from machine import Pin, WDT

# Buffer for exceptions from IRQ contexts (BLE). Without it, the error message is lost
# if it is no longer possible to allocate in the IRQ.
try:
    micropython.alloc_emergency_exception_buf(128)
except AttributeError:
    pass

from ble import BLEManager
from cfg import (CONFIG, FACTORY_RESET_HOLD_MS, FACTORY_RESET_PIN,
                 FACTORY_RESET_WINDOW_MS, SAFE_MODE_UPTIME_SEC,
                 SETUP_MODE_HOLD_MS,
                 bump_boot_count, save_flight_recorder, previous_flight_recorder,
                 log, log_boot, mark_planned_reset, reset_boot_count,
                 reset_cause_name, safe_mode_active, button_held,
                 factory_reset)
from fanout import NmeaSenderTask, nmea_tcp_task
from field_diagnostics import recorder as field_recorder
from esptel import EspTelemetryTask
from discovery import discovery_task
from gnss import GNSSHandler, StatusLED, send_command
from net import NetworkManager, NtripClient
from state import (SimpleQueue, _instances, app, shutdown_event,
                   stale_heartbeat, supervise)
from tracking import tracker
from web import http_server_task
from parallel_benchmark import prepare as prepare_parallel_benchmark, producer as parallel_producer


# ---------------------------------------------------------------------------
# ZENTRALE TASKS
# ---------------------------------------------------------------------------

async def watchdog_task():
    if not CONFIG.get("wdt_enabled", True):
        log("INFO", "SYS", "Watchdog disabled by configuration (development mode).")
        while not shutdown_event.is_set():
            app.stats["uptime_sec"] = app.uptime_seconds()
            await asyncio.sleep(2)
        return

    # NOTE: The ESP32-WDT is NOT switched off after activation. After shutdown_event,
    # the feeder stops -> hard reset within wdt_timeout. /save reboot is wanted
    # (machine.reset comes before anyway); Ctrl-C is unavoidable.
    wdt = WDT(timeout=CONFIG["wdt_timeout"])
    timeout_sec = CONFIG.get("heartbeat_timeout_sec", 60)
    log("INFO", "SYS", "Watchdog enabled (Heartbeat-Grenze %ss)." % timeout_sec)
    gemeldet = None
    while not shutdown_event.is_set():
        uptime = app.uptime_seconds()
        app.stats["uptime_sec"] = uptime

        # The start is considered successful after sufficient running time - only then the
        # boot counter clears its pending crash suspicion.
        if uptime > SAFE_MODE_UPTIME_SEC and app.stats["boot_count"]:
            reset_boot_count()
            app.stats["boot_count"] = 0

        # Only feed when all tasks give signs of life, otherwise the WDT runs out and
        # the device restarts - with documented cause.
        stalled_task = (stale_heartbeat(timeout_sec)
                        if timeout_sec and not app.maintenance_reason else None)
        if stalled_task is None:
            wdt.feed()
            gemeldet = None
        elif stalled_task != gemeldet:
            gemeldet = stalled_task
            log("ERROR", "SYS", "Task '%s' without a heartbeat for >%ss - "
                         "the watchdog will reset the device."
                % (stalled_task, timeout_sec))
            app.log_error("WATCHDOG", "Task '%s' is stalled." % stalled_task)

        await asyncio.sleep(2)
    log("INFO", "SYS", "Watchdog task stopped (WDT remains active until reset).")

async def gc_task():
    log("INFO", "SYS", "GC task started.")
    while not shutdown_event.is_set():
        await asyncio.sleep(CONFIG["gc_interval_ms"] / 1000)
        app.beat("gc")
        gc.collect()
        log("DEBUG", "GC", "Free RAM: %d Bytes" % gc.mem_free())
    log("INFO", "SYS", "GC task stopped.")

# ---------------------------------------------------------------------------
# GRACEFUL SHUTDOWN TASK
# ---------------------------------------------------------------------------

async def shutdown_handler(ble_mgr, gnss):
    await shutdown_event.wait()
    log("INFO", "SYS", "Asynchronous shutdown initiated; waiting for tasks...")

    await asyncio.sleep_ms(500)

    # 1. BLE deaktivieren
    try:
        ble_mgr.ble.active(False)
        log("INFO", "SYS", "BLE disabled.")
    except Exception as e:
        log("ERROR", "SYS", "BLE shutdown failed: %s" % e)

    # 2. UART schliessen
    try:
        gnss.uart.deinit()
        log("INFO", "SYS", "GNSS UART closed.")
    except Exception as e:
        log("ERROR", "SYS", "UART deinitialization failed: %s" % e)

    log("INFO", "SYS", "Cleanup complete; stopping event loop.")

async def check_access_button():
    """BOOT key: hold for setup, hold for factory reset. The key is scanned FIRST once: if it is not pressed, the function returns immediately. Only then a normal start costs not a second extra - is serviced only when someone actually presses. Operation: press RST, then immediately press BOOT and hold. If you hold BOOT when the voltage is applied, the chip starts in the ROM bootloader and this code does not run in the first place.
    """
    try:
        button = Pin(FACTORY_RESET_PIN, Pin.IN, Pin.PULL_UP)
    except Exception as e:
        log("DEBUG", "SYS", "Could not read BOOT button: %s" % e)
        return False

    if button.value() != 0:
        return False                        # Not printed, no stay

    log("WARN", "SYS", "BOOT pressed: %.1f s for setup, %.1f s for reset."
        % (SETUP_MODE_HOLD_MS / 1000, FACTORY_RESET_HOLD_MS / 1000))
    values = [0]
    for _ in range(FACTORY_RESET_WINDOW_MS // 100):
        await asyncio.sleep_ms(100)
        values.append(button.value())
        if button_held(values, FACTORY_RESET_HOLD_MS // 100):
            factory_reset()
            return "reset"
        if button.value() != 0:
            break
    if button_held(values, SETUP_MODE_HOLD_MS // 100):
        log("INFO", "SYS", "Setup mode requested with BOOT button.")
        return "setup"
    log("INFO", "SYS", "BOOT released too early; no action.")
    return None


async def check_factory_reset():
    """Compatibility for existing tools and tests."""
    return await check_access_button() == "reset"


async def tracking_startup_task():
    """Recover cooperatively while access, GNSS and correction tasks run."""
    started = time.ticks_ms()
    app.stats["tracking_startup"] = {"state": "loading"}
    try:
        await tracker.initialize_async()
        parallel_tracker, parallel_config = prepare_parallel_benchmark(tracker)
    except Exception as error:
        # Keep access available, but do not expose a partly recovered survey.
        tracker._loaded = False
        app.stats["tracking_startup"] = {"state": "error"}
        log("ERROR", "TRACK", "Survey recovery failed: %s" % error)
        while not shutdown_event.is_set():
            app.beat("tracking")
            await asyncio.sleep(1)
        return
    elapsed = time.ticks_diff(time.ticks_ms(), started)
    app.stats["tracking_startup"] = {"state": "ready", "duration_ms": elapsed}
    log("INFO", "TRACK", "Survey storage ready: %d points in %d ms." %
        (tracker.feature_count(), elapsed))
    tasks = [asyncio.create_task(tracker.run_worker())]
    if parallel_tracker is not None:
        _instances["parallel_tracker"] = parallel_tracker
        tasks.extend([
            asyncio.create_task(supervise("parallel_tracking", parallel_tracker.run_worker)),
            asyncio.create_task(supervise("parallel_benchmark",
                               lambda: parallel_producer(parallel_tracker, parallel_config))),
        ])
    await asyncio.gather(*tasks)


async def main():
    ursache_code = machine.reset_cause()
    ursache = reset_cause_name(ursache_code)
    boots = bump_boot_count(ursache_code)
    safe = safe_mode_active(boots)
    app.stats["boot_count"] = boots
    app.stats["reset_cause"] = ursache
    app.stats["safe_mode"] = safe
    app.stats["tracking_startup"] = {"state": "error" if safe else "loading"}

    print("\n==============================================")
    print("=== %s ===" % CONFIG["version"])
    print("==============================================")
    # The entry into any troubleshooting after a field failure: without this
    # information, you never know whether the watchdog, a panic reset or someone on the
    # plug has restarted the device.
    #
    # On USB, however, the line is often lost - the native CDC re-enumerates at the
    # reset and is not there yet when it is printed here, so the same is in the
    # persistent error log and under /status.
    summary = ("Start: cause %s, failed boots %d%s"
               % (ursache, boots, ", SAFE MODE" if safe else ""))
    log("INFO", "SYS", summary)
    log_boot(summary + " | " + CONFIG["version"])

    # Above all else: did someone print the BOOT button?
    button_action = await check_access_button()
    if button_action == "reset":
        log("WARN", "SYS", "Restarting after factory reset.")
        mark_planned_reset()
        await asyncio.sleep(1)
        machine.reset()
    elif button_action == "setup":
        app.stats["force_setup"] = True

    # If the last reset looked like a crash (watchdog, panic - not plugging in or a
    # self-dissolved restart), then back up the history from the RTC memory once into
    # the flash. The RTC memory survives the reset, but no power failure; so the
    # incident remains traceable even if someone later pulls the plug.
    if boots > 0 and previous_flight_recorder():
        log("WARN", "SYS", "Previous boot appears to have crashed; saving the "
                           "preceding flight recorder to the error log.")
        save_flight_recorder()

    # Recording must resume before the first network connection attempt.
    try:
        field_recorder.initialize()
        _instances["field_diagnostics"] = field_recorder
    except (OSError, ValueError) as error:
        log("ERROR", "DIAG", "Field recorder initialization failed: %s" % error)

    if safe:
        # The device couldn't go beyond SAFE_MODE_UPTIME_SEC.
        log("ERROR", "SYS", "Safe mode: %d consecutive boots did not reach %ds uptime. "
                     "NTRIP, BLE, and GNSS remain disabled."
            % (boots, SAFE_MODE_UPTIME_SEC))
        app.log_error("SYS", "Safe mode after %d boot attempts." % boots)
        gc.collect()
        network_manager = NetworkManager()
        tasks = [
            asyncio.create_task(supervise("field_diagnostics", field_recorder.run)),
            asyncio.create_task(supervise("watchdog", watchdog_task)),
            asyncio.create_task(supervise("gc", gc_task)),
            asyncio.create_task(supervise("network", network_manager.run)),
            asyncio.create_task(supervise("http", http_server_task)),
            asyncio.create_task(supervise("discovery", discovery_task)),
        ]
        await asyncio.gather(*tasks)
        return

    # Survey files are opened by the cooperative startup task after access starts.
    nmea_queue = SimpleQueue(maxsize=CONFIG.get("nmea_queue_size", 256))

    # RELATED IS CRITICAL: WLAN first, then BLE.
    #
    # Beide Funkteile bedienen sich am ESP-IDF-Heap - gemessen rund 42 KB
    # for Wi-Fi and 62 KB for BLE. From the same pot also the MicroPython-Heap, and the
    # grows with the size of main.py. If BLE was created first, there was too little
    # left for Wi-Fi and the start broke off with "OSError: WiFi Out of Memory". BLE
    # behaves scarcity better than the Wi-Fi driver, so WLAN gets the first access.
    gc.collect()
    network_manager = NetworkManager()
    _instances["network"] = network_manager
    gc.collect()

    ble_mgr = BLEManager((app.identity or {}).get("ble_name"))
    gnss = GNSSHandler(nmea_queue)
    _instances["ble"] = ble_mgr
    _instances["gnss"] = gnss

    # Write accesses to the BLE-RX characteristic go to the receiver as a verified
    # command, and without this assignment, RX is ineffective.
    ble_mgr.command_sink = lambda sentence: send_command(gnss.uart, sentence)

    ntrip = NtripClient(gnss.uart)
    _instances["ntrip"] = ntrip
    ble_sender = NmeaSenderTask(ble_mgr, nmea_queue)
    telemetry = EspTelemetryTask(nmea_queue, ntrip)

    # supervise() gets factories, not coroutines: an already consumed coroutine cannot
    # be expected again.Until V9.3 the tasks ran naked in gather() - an exception in any
    # one ended the entire program.
    tasks = [
        asyncio.create_task(supervise("field_diagnostics", field_recorder.run)),
        asyncio.create_task(shutdown_handler(ble_mgr, gnss)),
        asyncio.create_task(supervise("watchdog", watchdog_task)),
        asyncio.create_task(supervise("gc", gc_task)),
        asyncio.create_task(supervise("led", StatusLED().run)),
        asyncio.create_task(supervise("network", network_manager.run)),
        asyncio.create_task(supervise("ntrip", ntrip.run)),
        asyncio.create_task(supervise("http", http_server_task)),
        asyncio.create_task(supervise("discovery", discovery_task)),
        asyncio.create_task(supervise("gnss", gnss.run)),
        asyncio.create_task(supervise("ble", ble_sender.run)),
        asyncio.create_task(supervise("ble_output", ble_sender.run_ble)),
        asyncio.create_task(supervise("tcp_output", ble_sender.run_tcp)),
        asyncio.create_task(supervise("ble_events", ble_mgr.event_task)),
        asyncio.create_task(supervise("nmea_tcp", nmea_tcp_task)),
        asyncio.create_task(supervise("telemetry", telemetry.run)),
        asyncio.create_task(supervise("tracking", tracking_startup_task)),
    ]

    await asyncio.gather(*tasks)


# The guard keeps the file importable under CPython (tests/), without changing the
# behavior on the board: MicroPython executes main.py as __main__, so the block runs
# there as before.
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("INFO", "SYS", "Interrupted by user (Ctrl-C). performing hard shutdown...")

        try:
            if "gnss" in _instances:
                _instances["gnss"].uart.deinit()
                log("INFO", "SYS", "GNSS UART closed manually (Hard Shutdown).")
        except Exception:
            pass

        try:
            if "ble" in _instances:
                _instances["ble"].ble.active(False)
                log("INFO", "SYS", "BLE disabled manually (Hard Shutdown).")
        except Exception:
            pass

    finally:
        asyncio.new_event_loop()
        log("INFO", "SYS", "Program stopped.")

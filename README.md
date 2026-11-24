# P-mag Configurator

Read and set the timing on an E-MAG / P-MAG electronic ignition from a laptop,
over an RS-232 serial cable. Choose your engine and the tool works out the
values to program. It checks that the engine is stopped, writes the settings,
reads them back to confirm, and keeps a log.

> **Not affiliated with or endorsed by E-MAG Ignitions.** Check every setting
> against your engine and P-mag documentation before flight. Use at your own risk.

## Use it in your browser (no install)

Open the page in **Chrome or Edge** on a Windows, Mac, Linux or ChromeOS
computer: **https://dnickerson.github.io/P-mag/**

1. Plug in your USB-to-serial adapter and connect it to the P-mag (see wiring below).
2. Click **Connect to P-mag…** and pick the adapter's port.
3. Click **Read from P-mag** to see the current settings.
4. Choose your engine (or *Create a Custom Configuration*), check the preview,
   then click **Write to P-mag**.

Tick **Simulate (no hardware)** to try the page without a P-mag.

The log is kept in your browser. Use **Download CSV** to save a copy.

Safari, Firefox, iPad and iPhone are not supported: they can't open serial ports.

## Wiring

| E-MAG 6-pin connector | Signal |
|---|---|
| Pin 1 | Signal ground |
| Pin 2 | Tx (P-mag output) |
| Pin 3 | Rx (P-mag input) |

Serial settings: 9600 baud, 8 data bits, no parity, 1 stop bit.

## Engine settings

| # | Engine | Takeoff timing | Cruise max advance | RPM limit |
|---|---|---|---|---|
| 2 | Low Compression O-320 150 HP - Break-in (Parallel Valve) | 25.2° | 25.2° | 3072 |
| 3 | Low Compression O-320 150 HP (Parallel Valve) | 25.2° | 32.2° | 3072 |
| 4 | Standard Compression (I)O-320 160 HP - Break-in (Parallel Valve) | 25.2° | 25.2° | 3072 |
| 5 | Standard Compression (I)O-320 160 HP (Parallel Valve) | 25.2° | 32.2° | 3072 |
| 6 | Standard Compression (I)O-360 180 HP - Break-in (Parallel Valve) | 25.2° | 25.2° | 3072 |
| 7 | Standard Compression (I)O-360 180 HP (Parallel Valve) | 25.2° | 32.2° | 3072 |
| 8 | Standard Compression (I)O-360/390 200 to 210 HP - Break-in (Angle Valve) | 19.6° | 21.0° | 3072 |
| 9 | Standard Compression (I)O-360/390 200 to 210 HP (Angle Valve) | 21.0° | 25.2° | 3072 |
| 10 | Create a Custom Configuration | 15.4–37.8° | 15.4–46.2° | 2816–3584 |
| 11 | P-mag Default Settings | 26.6° | 39.2° | 3328 |

What gets programmed into the P-mag:

- **AdvShft** = takeoff timing − 26.6
- **MaxAdv** = cruise max advance − 1.4 (the P-mag advances 1.4° past this setting)
- **RPM limit** in 256-rpm steps

## Commands sent

```
/*        wake up
/x        read RPM (must be 0)
/?        read firmware version
/C-14     set AdvShft   (tenths of a degree)
/N238     set MaxAdv    (tenths of a degree)
/M3072    set RPM limit
/W        save to flash
/c /n /m  read the settings back
```

**Check this against your firmware before your first write.** Some P-mag
documentation describes `/C` and `/N` in 1.406° steps and `/M` in ×256 blocks
instead. The **Serial activity** panel shows every command and reply. If your
firmware differs, the encoding is set in a few small functions near the top of
the script in `index.html` (and in `pmag_configurator.py`).

## Command-line version (Python)

`pmag_configurator.py` does the same job from a terminal.

```
pip install pyserial
python pmag_configurator.py                    # interactive menu
python pmag_configurator.py engines            # list engines
python pmag_configurator.py read  --port COM8
python pmag_configurator.py write --port COM8 --engine 6 --dry-run
python pmag_configurator.py read  --simulate   # no hardware
```

Its log is saved to `pmag_log.csv` next to the script.

## Publishing the web page

In the repository on GitHub: **Settings → Pages → Build and deployment →
Deploy from a branch**, choose `main` and `/ (root)`. The page is served
from `index.html`. Web Serial needs HTTPS, which GitHub Pages provides.

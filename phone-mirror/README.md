# Phone Mirror

See and control your Android phone's screen from your laptop over USB, like Vysor,
but **nothing gets installed on the phone**. This is for using a phone whose screen
is broken or black.

It works through `adb`, which runs on the laptop, and tools that already ship with
every Android phone:

| What                | Phone tool used (built in) |
|---------------------|----------------------------|
| See the screen      | `screencap` (default) or `screenrecord` (`--fast`) |
| Tap / swipe / type  | `input` |

The page opens in your browser. Click to tap, drag to swipe, and type on your
laptop keyboard to type on the phone.

---

## 1. One-time setup (on the laptop)

1. **Python 3.8+**: https://www.python.org/downloads/ (on Windows, tick *"Add python to PATH"*).
2. **adb** (Android SDK Platform-Tools):
   https://developer.android.com/tools/releases/platform-tools.
   Unzip it so the `platform-tools` folder sits **next to `mirror.py`**, or put it on your PATH.
3. *(Optional, for smooth video)* **ffmpeg**: https://ffmpeg.org/download.html.
4. Windows only: if `adb devices` doesn't show your phone, install your phone maker's
   USB driver (e.g. Samsung USB Driver, or Google USB Driver for Pixels).

## 2. Run it

Plug the phone in with a **data** USB cable (some cheap cables only charge), then:

```bash
cd phone-mirror
python mirror.py            # Windows: double-click start-windows.bat
```

Your browser opens `http://127.0.0.1:8765` showing the phone.

```bash
python mirror.py --fast           # smoother (20-60 fps), needs ffmpeg
python mirror.py --list           # show connected phones
python mirror.py --allow SERIAL   # lock the tool to your own phone(s)
```

### Lock it to your phones only
Run `python mirror.py --list`, then `python mirror.py --allow <your serial>` once for
each of your phones. The serials are saved in `allowed_devices.txt`, and from then on the
tool refuses to use any other phone. The web page only listens on `127.0.0.1`, so nobody
else on your Wi-Fi can reach it.

### Controls
| Laptop                     | Phone |
|----------------------------|-------|
| Click                      | Tap |
| Click and drag             | Swipe (also draws pattern locks) |
| Click and hold             | Long press |
| Scroll wheel               | Scroll |
| Right-click / `Esc`        | Back |
| Typing                     | Types into the focused text field |
| Buttons in the side panel  | Home, Back, Recents, Volume, Power, Wake + unlock, Screenshot |

---

## 3. Broken screen? Read this

Android only lets a computer control the phone when **USB debugging** is on **and**
you've tapped **"Allow"** for that computer. No app (Vysor, scrcpy or this one) can get
around that; it's a security feature. Run `python mirror.py --list`:

* **`device`**: you're good. Run `python mirror.py`.
* **`unauthorized`**: debugging is on, but the phone is waiting for you to tap *Allow*
  on its screen. See the options below.
* **Nothing listed**: USB debugging is off (or the cable/driver is bad). See the options below.

If you can't see the screen to turn these on, try these in order:

1. **Partly working screen or touch.** Even a cracked or dim screen may be enough
   to tap through:
   *Settings → About phone → tap "Build number" 7 times → Developer options → USB debugging*,
   then plug in and tap **Allow** (tick *"Always allow from this computer"*).
2. **USB-C to HDMI adapter (no software at all).** Many phones (Samsung Galaxy S8 and newer, many
   newer Androids) output video over USB-C to a TV/monitor. Use that screen to turn on
   USB debugging and tap *Allow*, then switch to this tool.
3. **USB OTG mouse.** Touch dead but the display is visible? Plug a mouse in with an
   OTG adapter and a pointer appears.
4. **Samsung only.** *Samsung Flow*, *Smart Switch* (backup) or *SmartThings Find*
   (unlock) can help recover data without the screen.
5. **Blind tapping.** Some people manage the "Allow" prompt by plugging in, waiting,
   then pressing *Tab / Enter* with an OTG keyboard. Results vary by phone.

Once you've tapped *Allow* with "Always allow" ticked, this laptop is trusted from then
on and the tool works even when the screen is completely black.

---

## Troubleshooting

* **Black image with the phone locked/asleep**: click **Wake & swipe up**, then enter your PIN.
* **Some apps show black** (banking, Netflix): they block screen capture on purpose.
* **`--fast` shows nothing**: a few phones block `screenrecord`; run without `--fast`.
  In `--fast` mode, taps can land in the wrong spot if the phone is rotated to
  landscape. Use the normal mode for landscape apps.
* **Typing special characters**: `input text` only handles plain ASCII. For emoji or
  accented letters, use the phone's keyboard by clicking on it.
* **Port in use**: `python mirror.py --port 9000`.

# PATac
Post Apo Tycoon autoclicker


[Features](#-features)

[Install](#-install)

[How to use](#-how-to-use)

[Known bugs](#-known-bugs)

[License](#-license)


<br>

## ✅ Features
1. Mode to click listed settlements (will click non-main settlements that are invisible on max zoom).
2. Mode to send up to 30 clicks per second into pointer location.
3. Mode to draw a polygon and send specified count of clicks per second in randomized points inside this polygon (useful for some in-game events).
4. GUI/TUI.
5. Automatic ADB source configure.
6. Windows and MacOS crossplatform.
7. Can work in background (using ADB).
8. All settings stored in .json files in script folder.
9. User-friendly calibration of game [x;y] into ADB [x;y].
10. On-fly offset changes with hotkeys for better clicking accuracy.


<br>

## ✅ Install
>Requires Bluestacks (or Bluestacks Air for MacOS) with enabled ADB (settings > advanced) and "Show pointer location" to emulate android on PC.

### Windows
Copy script files from this repo (better in separate folder) in any place or use this command and unzip.

```
curl -L -O https://github.com/skyfire-e/PATac/releases/download/v1.0.4/PATac1.0.4.zip
```
Install Python in terminal (win+X > terminal (admin) )
```
winget install Python.Python.3.12
```

Re-launch terminal, update pip

```
python -m pip install --upgrade pip
```

Install Tesseract

```
winget install UB-Mannheim.TesseractOCR
```

Set Path for Tesseract (if installed into default folder)

```
setx PATH "%PATH%;C:\Program Files\Tesseract-OCR"
```

Install Python dependencies

```
pip install pynput numpy Pillow pytesseract
```


### MacOS

Isntall Homebrew
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Install Python, Tesseract, Tkinter
```
brew install python@3.12 android-platform-tools tesseract python-tk@3.12
```
Set PATH for Python
```
echo 'export PATH="/opt/homebrew/opt/python@3.12/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Re-launch terminal, create folder for PATac
```
mkdir -p ~/PATac && cd ~/PATac
```
Create virtual environment (venv)
```
python3 -m venv venv
```
Activate venv
```
source venv/bin/activate
```
Update pip inside venv
```
pip install --upgrade pip
```
Install Python dependencies in venv
```
pip install pynput numpy Pillow pytesseract pyobjc-framework-Quartz pyobjc-framework-Cocoa
```
Copy script files from this repo in PATac folder or use this command and unzip.

```
curl -O ~/PATac "https://github.com/skyfire-e/PATac/releases/download/v1.0.4/PATac1.0.4.zip"
```


<br>

## ✅ How to use

### 1. Launch

For Windows navigate in terminal (using cd) into folder where you copied repo files and run ```python pat_clicker_gui.py``` for GUI version or ```python pat_clicker.py``` for TUI version.

For MacOS in terminal use for GUI (swap ```python pat_clicker_gui.py``` for ```python pat_clicker.py``` for TUI)
```
cd ~/PATac
source venv/bin/activate
python3 python pat_clicker_gui.py
```
Or create alias
```
alias PATac='cd ~/post_apo_clicker && source venv/bin/activate && python python pat_clicker_gui.py'
```
For next launches just type in terminal
```
PATac
```

### 2. Initial Setups (for GUI, for TUI just type corresponding menu number)
2.1. Settings, Zoom Out and swipe calibration
>   After pressing this button, hold left click over "Settings" button in Post Apo Tycoon and type ADB X and Y coordinates of the Settings button into pop up window. Then do the same for Zoom Out button (that appears after clicking Settings). Next hold left click in the middle (but try to hold NOT on settlements) and type ADB coordinates for start of swipe. Then drag map to allign bottom main settlement in an area of "Claim" button that appears in daily reset. That way when it appears and pause your farming, it will be automatically clicked through. Type second coordinates where the mouse cursor is located after this drag.

2.2. Calibrate
>   In Post Apo Tycoon press [X] to close building menu (if persist). Make sure that there is only 4 bottom buttons (stats, upgrades, gemshop, settings) and they are alligned in the middle (via game options).
>   Press Calibrate button and confirming with OK game will do subsequence of settings > zoom out > drag. After this select with transparent polygon zoomed out game map borders (e.g. put polygon smaller than game map within it borders). After this script will do subsequence of clicks. After each tap script will ask to draw a rectangle over game [x: , y: ] coordinates over game window in blue stacks. It is located in the header of building menu just to the left of [X] close button of building menu. Try to draw polygon with a couple of pixels to spare around game x and y. After you draw polygon wait a little for script to send another tap and coordinates to change. When script will finish it sends confirmation window.

2.3. Clicker options
>   Type desired clicks per second that will be sent across ALL settlements (e.g. for 5 clicks per second across 5 points input 25 here).

2.4. Points where to click
>   Script comes with pre-recorded 5 main settlements game X and Y coordinates. You can press Edit points list button and add/edit/delete desired points to click. You can also edit points.json directly. Use game coordinates in this settings.



### 3. Using Main Clicker
> In Post Apo Tycoon press [X] to close building menu (if persist). Make sure that there is only 4 bottom buttons (stats, upgrades, gemshop, settings) and they are alligned in the middle (via game options).

Press Start Main Clicker. Press ```Shift + 1``` to launch subsequence settings > zoom out > drag map > automatically send 1 tap into game [x:1,y:1] and OCR verify calibrations > after 1 sec starts sending left clicks into points. Press ```Shift + 1``` again to stop.  

### 4. Using left clicks spam
Press Enter to Hold-to-Spam. While this active, holding side mouse button OR middle mouse button will spam left clicks where cursor is located.

### 5. Polygon Clicker
After pressing button draw a polygon where you want to randomly send left clicks with transparent overlay. After that determine clicks per second. Press ```Shift + 2``` and script will send clicks randomly within selected area. Press ```Shift + 2``` to stop.

### 5. Live ADB offset...
If script missing some settlements or have bad accuracy for spamming you can use arrow keys on keyboard or buttons in this menu to calibrate dX and dY offsets on fly to increase accuracy.


<br>

## ✅ Known bugs

1. After consecutive hours of clicking ADB may bug out and will not stop sending clicks after pressing Shift + 1 (or closing script). Re-launch Bluestacks.
2. Feature with hold-to-spam can be off-cursor position. Can be edited with Live ADB offset on fly.


<br>

## ✅ License
Use at your own risk. Watch out for missclick and gem spending. Do not violate any game rules. 

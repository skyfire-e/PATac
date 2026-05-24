# PATac
Post Apo Tycoon autoclicker


[Features](#-features)

[Install](#-install)

[How to use](#-how-to-use)


## ✅ Features
1. Windows and MacOS crossplatform.
2. Can work in background (using ADB).
3. GUI/TUI.
4. Mode to click listed settlements (will click invisible on max zoom non-main settlements).
5. Mode to send 30 clicks per second into pointer location.
6. Mode to draw a polygon and send randomized clicks per second in randomized location.
7. User-friendly calibration of game [x;y] into ADB [x;y].
8. All settings stored in .json files in script folder.

## ✅ Install
Requires Bluestacks (or Bluestacks Air for MacOS) with enabled ADB (settings > advanced) and "Show pointer location" to emulate android on PC.

### Windows
Copy script files from this repo (better in separate folder) in any place.

Install Python in terminal (win+X > terminal (admin) )
```
winget install Python.Python.3.12
```

Re-launch terminal, install pip

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
Copy script files from this repo in PATac folder.


## ✅ How to use

For Windows navigate in terminal (using cd) into folder where you copied repo files and run ```python pat_clicker_gui.py``` for GUI version or ```python pat_clicker.py``` for TUI version
For MacOS
cd ~/PATac
source venv/bin/activate
python3 PATac.py
```
alias PATac='cd ~/post_apo_clicker && source venv/bin/activate && python pat_clicker.py'
```
Next launches just type in terminal
```
PATac
```



## ✅ Known bugs

## ✅ License
Use at your own risk. Watch out for missclick and gem spending. Do not violate any game rules. 

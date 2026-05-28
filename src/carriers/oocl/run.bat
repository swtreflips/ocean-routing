@echo off
:: Activate the Anaconda environment
call C:\Users\LuisMiguelHernandezT\anaconda3\Scripts\activate.bat patch

:: Change to the directory where this .bat file is located
cd /d "%~dp0"

:: Run the script using the activated environment
python main.py

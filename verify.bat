@echo off
cd /d %~dp0
py asm68k.py tests\test1.asm -o _check1.c --check tests\test1.c
py asm68k.py tests\test2.asm -o _check2.c --check tests\test2.c
py asm68k.py tests\test3.asm -o _check3.c --check tests\test3.c
py asm68k.py tests\test4.asm -o _check4.c --check tests\test4.c
echo.
echo All four above should say "check OK: identical".
pause

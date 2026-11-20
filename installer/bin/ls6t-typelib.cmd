@echo off
rem ls6t-typelib - lists the commands of LabSpec's ActiveX (installed by ls6t-setup)
"%~dp0..\python\python.exe" -m labspec_translator.typelib %*

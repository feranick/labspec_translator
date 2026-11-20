@echo off
rem ls6t - the labspec-translator command line (installed by ls6t-setup)
"%~dp0..\python\python.exe" -m labspec_translator.cli %*

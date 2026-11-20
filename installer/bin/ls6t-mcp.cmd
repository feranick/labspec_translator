@echo off
rem ls6t-mcp - the MCP server for the Claude app (installed by ls6t-setup)
"%~dp0..\python\python.exe" -m labspec_translator.mcp_server %*

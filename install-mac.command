#!/usr/bin/env bash
# Double-click installer for macOS: installs /godmode for Claude Desktop (Code tab) and the claude CLI.
cd "$(dirname "$0")" || exit 1
./install.sh "$@"
status=$?
echo
if [ $status -eq 0 ]; then
  echo "All done. Quit Claude Desktop completely (Cmd+Q), reopen it, open the Code tab and type /godmode"
else
  echo "Installation failed (exit $status). See the messages above."
fi
read -r -p "Press Enter to close this window..." _
exit $status

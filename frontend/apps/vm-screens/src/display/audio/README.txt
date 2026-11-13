Interaction display audio asset

The repository includes an original, loopable demo track at:
  src/display/audio/interaction-theme.mp3

Replace this file with the approved deployment audio before production use if needed.

DisplayAudio.tsx imports the file, so the build emits it as
assets/interaction-theme-<hash>.mp3. Keep it under src/ and imported:
the VM guest app and the dom0 phone relay serve only /assets/*.mp3,
so a fixed public/ URL such as /audio/... returns 404 on the board.

Set VITE_DISPLAY_AUDIO_SRC at build time to use another URL; it must also
resolve under /assets/ to play on the board.
Set VITE_DISPLAY_AUDIO_AUTOPLAY=1 only when the dom0 Chromium policy permits autoplay.

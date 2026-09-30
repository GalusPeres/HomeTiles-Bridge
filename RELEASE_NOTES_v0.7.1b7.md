# HomeTiles Bridge v0.7.1b7 (beta)

Camera popups stay live on slower Home Assistant hosts.

- On some hosts the Bridge decoded a camera stream more slowly than real time, for example a 1440p60 stream in a Home Assistant VM. The picture fell further and further behind and then smeared, because the stream server dropped data for the slow reader. Panels with a larger camera picture (8-inch, 10.1-inch) were hit hardest.
- The Bridge now decodes camera streams on four threads instead of one.
- If it still falls more than one second behind, it restarts the stream at the live position. If that happens twice within a minute, the popup shows only the stream's key frames until it is closed: fewer pictures per second, but current and never smeared. The Home Assistant log then says so and suggests a smaller camera stream.
- Still-image cameras and the live view of a panel's own camera are unchanged.
- Everything else from v0.7.1b6 is unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 403 Bridge tests. Checked with FFmpeg against a live 1440p60 RTSP stream: no restart at normal speed, and a reader throttled to 0.8x real time was restarted after 8.5 s.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b6...v0.7.1b7

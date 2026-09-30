# HomeTiles Bridge v0.7.1b8 (beta)

Camera popups keep up with large camera streams again, without the one-picture-per-second fallback of b7.

- v0.7.1b7 did not fix slow camera popups: the Bridge still fell behind a 1440p60 stream, and after two restarts the popup showed only key frames, one picture per second. That fallback is removed.
- The actual bottleneck was scaling: the Bridge scaled every frame of a 60 FPS stream to the panel size on a single thread and only then dropped most of them. It now keeps only the frames the panel shows (at most 24 per second) before scaling, and uses a cheaper scaler for the large reduction. For the same 1440p60 stream this takes about a third of the time.
- A stream that still falls more than one second behind is restarted at the live position. The Home Assistant log shows this once per popup.
- Camera streams keep decoding on four threads. Sources at 24 FPS or less keep all their frames.
- Still-image cameras and the live view of a panel's own camera are unchanged.

Install: in HACS enable "Show beta versions" for HomeTiles Bridge, update, restart Home Assistant and reload the page with Ctrl+Shift+R.

Validation: 402 Bridge tests. FFmpeg on a recorded 1440p60 RTSP stream: 1.1 s instead of 3.6 s for 20 s of video; 60 and 30 FPS sources give 24 pictures per second, a 10 FPS source keeps 10.

**Full Changelog:** https://github.com/GalusPeres/HomeTiles-Bridge/compare/v0.7.1b7...v0.7.1b8

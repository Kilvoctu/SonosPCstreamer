# SonosPCstreamer
Simple tool to watch videos with audio going to Sonos speakers 

<img src="preview.png" alt="Preview" width="450">

I made this tool after [my YT streamer](https://github.com/Kilvoctu/SonosYTstreamer), mainly meant for watching video files. I ended up adding some YT functions into it though. This is a quickly made tool for personal use, don't expect anything polished or even fully functional.


### Setup
- Have Python 3.10 at least
- Know your speaker IP address
- Run `run.bat`. It will set up the .env, create a local Python environment, download ffmpeg and mpv, then run the app.

### Usage
- Run `run.bat`. 
- Browse to a video file or paste a YT link 
- Check audio track, etc.
- Click Play.
- Hope that an MPV window opens with the video.

### Features
- Supports multiple speakers (one speaker is designated as the coordinator).
- Supports most video files, online content and live streams (anything that's support by yt-dlp).
- A/V syncs by adjusting video speed until it's synced.
- Can select audio tracks and subtitles, and can seek timestamp and change volume.
- Supports SDR/HDR

### Known issues and other things
- Really janky. Things will randomly not work (playback, seeking, color space, etc.).
- Audio doesn't always sync properly, and it's not perfectly in sync even when it is (roughly within 200ms).
- Doesn't play age-restricted YT content.
- Probably a bunch of bugs I haven't caught or tested for.
- Only tested on a pair of Sonos One with an HDR display.

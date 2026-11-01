# MFD Media Downloader Bot

A personal Telegram bot for downloading public media links. 

It shows available video and audio formats, estimated sizes and a preview, then queues the chosen file and reports its progress. 

Completed files can be sent in Telegram or opened through a temporary server link.

The bot supports links handled by `yt-dlp`, including many YouTube, Instagram, TikTok, VK and Facebook posts. Actual availability depends on the source and the permissions for each post. It can prepare H.264/AAC video for Apple devices, extract audio as M4A or MP3, and work in private chats and enabled groups. In groups, members use `/dd` followed by a link.

The interface offers English and Russian. Users can revisit their downloads in **My Files** and change download, compatibility and caption settings. The owner has an admin panel for service health, storage, history, the changelog and Bug Reports. A screenshot captioned `#bugreport` is saved as a numbered private ticket; see [BUGREPORTS.md](BUGREPORTS.md). Downloads and conversions are processed one at a time to fit a small server.

See [INSTALLATION.md](INSTALLATION.md) for setup, configuration, hosting and local Telegram Bot API instructions. 

See [CHANGELOG.txt](CHANGELOG.txt) for release history. The owner can open the newest entry from the button on each successful deployment notice.

For a current project snapshot and outstanding work, see [PROJECT_STATE.md](PROJECT_STATE.md) and [BACKLOG_TASKS.md](BACKLOG_TASKS.md). Bug Report storage and agent access are described in [BUGREPORTS.md](BUGREPORTS.md).

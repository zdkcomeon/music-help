# Music Help

一个本地运行的音频标签管理网页。可以导入文件或扫描指定音乐目录，编辑歌曲信息、封面和歌词，通过 MusicBrainz、Cover Art Archive 和 LRCLIB 查找在线资料。

## 启动

需要 Python 3.10 或更新版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Windows 下使用 `.venv\Scripts\activate` 激活虚拟环境。打开浏览器访问 `http://127.0.0.1:8765`。

如果音乐文件已经保存在运行服务的电脑上，可以指定音乐目录。网页中的服务器目录会递归扫描该目录中的音频文件。

```bash
python app.py --music-dir /path/to/music
```

## 使用

- 上传音频文件或文件夹，或者在服务器目录中选择歌曲。程序根据文件内容识别格式，并提示扩展名与实际格式不一致的文件。单次上传上限为 2 GB。上传文件保存在服务的临时目录中，保存后需要下载，服务重启后临时文件会被清理。
- 选中单首歌曲编辑标签、歌词和封面，或者多选歌曲批量修改。批量保存只应用修改过的字段。封面支持 JPEG 和 PNG，最大 10 MB。
- 点击在线匹配，搜索歌曲并选择候选结果。获取到的资料先填入编辑区，检查后点击保存。在线功能需要服务器能够访问对应服务。
- 选中歌曲后可以试听、按标签重命名或下载。浏览器支持的试听格式取决于浏览器本身。重命名时可以选择根据检测结果修正扩展名。多首歌曲会打包为 ZIP。服务器目录中的文件会直接保存和重命名，上传文件会在下载时使用新文件名。

写入时先在同一目录创建副本，完成标签修改后替换原文件。若文件在此期间被其他程序修改，保存会停止并提示重新读取。ID3v2.3 标签会继续保存为 v2.3，ID3v2.2 标签会转换为 v2.4。重命名遇到同名文件会跳过该文件。

支持 MP3、AAC、M4A/MP4、FLAC、Ogg Vorbis、Opus、WAV、AIFF、WMA、WavPack 和 True Audio。APE、Musepack、TAK、OptimFROG、Ogg FLAC 和 Ogg Speex 也支持标签读写。AAC 裸流使用 ID3 标签，不同播放器对这些标签的支持可能不同。在线匹配依赖公开数据库中的资料，无法保证每首歌都有候选、封面或歌词。服务运行期间，在线请求及其响应会记录在服务日志中，便于排查问题。

## 验证

```bash
python -m unittest discover -s tests -v
```

多格式回归测试使用系统 FFmpeg，或 Python 环境中的 `imageio-ffmpeg` 生成音频样本。缺少 FFmpeg 时，这部分测试会跳过。

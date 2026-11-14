# Audio Workbench

Python 音视频处理 HTTP 服务，提供 PCM 音频处理和持久化媒体转码任务。需要 Python 3.10 或更新版本，媒体引擎由 `imageio-ffmpeg` 提供。

## 启动

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m audio_workbench.server --host 127.0.0.1 --port 8765 --data-dir ./media-data
```

默认只监听本机；服务没有身份验证，勿直接暴露到不可信网络。`AUDIO_WORKBENCH_DATA` 可设置数据目录，`--data-dir` 优先；`--workers` 为 1–4，默认 2。一个数据目录只允许一个服务进程使用。

## 接口

均为 JSON POST，`audio` 是完整 WAV 文件的 base64 编码。支持 16 位未压缩 PCM、单声道或立体声、8–48 kHz、最长 30 秒。请求体上限 8 MiB。

* `/inspect`: `{"audio":"..."}`，返回采样率、声道数、帧数、时长和归一化峰值。
* `/clip`: `{"audio":"...","start":0.1,"end":1.2,"gain":1.0}`，时间单位为秒，区间左闭右开，返回 `audio` 和 `properties`。
* `/mix`: `{"tracks":[{"audio":"...","offset":0,"gain":1}]}`，最多 8 轨；默认格式必须相同，偏移单位为秒，输出最长 30 秒。可附加 `"format":{"sample_rate":48000,"channels":2}`，先对每轨做线性插值升采样与单、双声道转换；目标采样率不得低于任一源采样率。

增益范围为 0–4，混音结果饱和到 16 位范围。错误返回 HTTP 400 和 `{"error":"..."}`，未知路径返回 404。输出的 `audio` 可 base64 解码保存为 WAV。

### 持久化媒体任务

* `POST /jobs`: `{"source":"base64 媒体内容","preset":"mp4","start":0,"duration":5}`。`preset` 可选 `mp4`（H.264/AAC）或 `audio`（WAV），源文件最大 5 MiB，时长最多 60 秒；返回 HTTP 202 和任务对象。
* 时间线使用相同入口：`{"preset":"timeline","segments":[{"source":"base64 媒体内容","start":0,"duration":1}, {"source":"base64 媒体内容","start":2,"duration":1}]}`。每段需包含视频和音频，最多 8 段，总源数据不超过 5 MiB，总时长不超过 60 秒，单段不少于 1/30 秒。输出统一为 640×360、30 fps、48 kHz 双声道；画面按比例缩放并补黑边，片段不足时末帧和静音补齐。音轨或视频流缺失会使任务失败。
* `GET /jobs/{id}` 查询状态和错误；`GET /jobs/{id}/result` 在成功后返回 base64 的 `audio_video` 和 SHA-256。
* `POST /jobs/{id}/cancel` 请求取消，提交空 JSON 对象。排队任务立即取消，执行中的任务终止子进程后变为取消状态；已结束的任务无法取消。

任务和源文件保存在数据目录。进程重启后排队任务继续执行，原来执行中的任务标记为失败并清理临时结果；调用者可重新提交。产物先写入临时文件再保存，并在读取时校验摘要。服务不自动删除历史素材和产物，请在服务停止后自行维护数据目录。

## 验证与结构

```sh
python3 -m compileall -q audio_workbench
python3 -m audio_workbench.smoke inspect
python3 -m audio_workbench.smoke clip
python3 -m audio_workbench.smoke mix
python3 -m audio_workbench.job_smoke
```

`server.py` 负责 HTTP，`service.py` 编排和校验，`waveio.py` 读写音频，`processing.py` 处理样本，`jobs.py` 管理数据库和执行器；两个 smoke 模块经真实 HTTP 路径执行有限检查。新增编排业务由 unittest 套件验证；旧媒体路径继续使用真实 HTTP smoke。

不提供身份认证、媒体探测接口、自动重试、进度推送或分布式执行。

## 素材与工程编排（基线 v2）

工作区在数据目录下的 `workbench/`，使用 SQLite 元数据和不可变内容寻址 WAV。服务在启动任何队列之前获取工作区独占锁，第二个服务使用同一目录会失败。历史素材不自动回收。

- `POST /assets`：`{"name":"旁白","audio":"base64 WAV"}`，返回素材 ID、格式、帧数和 SHA-256。`GET /assets` 只列活跃素材，`?archived=true` 包含归档项；`GET /assets/{id}/content` 读取原始 WAV。
- `POST /assets/{id}/archive`：提交 `{}`。归档素材不再用于新的工程修订，但已有修订仍可读取和渲染它。
- `POST /projects`：提交名称和下面的 `document`。返回第 1 次修订。
- `POST /projects/{id}/revisions`：提交 `{"expected_revision":1,"document":{...}}`。只有预期修订仍为当前头修订时才能保存，冲突返回 409；历史修订不被覆盖。
- `GET /projects/{id}` 读取当前修订，`?revision=1` 读取历史；`GET /projects` 列出工程。
- `POST /projects/{id}/render`：提交 `{}` 渲染接受时的头修订，或 `{"revision":1}` 指定历史。返回 202。任务接受前冻结素材摘要、源片段和目标采样帧，随后编辑工程不会改变该任务。
- `GET /renders/{id}` 查询冻结计划、状态、进度和错误；`GET /renders/{id}/result` 返回 `audio`、`sha256`、`properties`、`project_id` 和 `revision`。`GET /renders` 返回最近 100 条，可按 `?project_id=...` 过滤。
- `POST /renders/{id}/cancel` 提交 `{}`。取消请求与结果发布串行裁决，已结束任务不能取消。关闭服务停止领取、打断执行并等待线程结束；重启保留排队任务，执行中断记录失败。
- `GET /health` 返回 PCM 队列线程与排队数。

编排文档示例：

```json
{
  "schema_version": 1,
  "format": {"sample_rate": 16000, "channels": 2},
  "master_gain": 0.8,
  "clips": [
    {"id": "voice", "asset_id": "素材返回的ID", "start": 0, "end": 1, "at": 0.5, "gain": 1, "muted": false}
  ]
}
```

片段的 `start`、`end` 和 `at` 使用秒；省略 `start` 默认 0，省略 `end` 使用整个源，其他默认值如示例（主增益默认为 1）。同一修订的片段 ID 唯一，最多 32 片段；输出上限 60 秒，输入素材仍为最长 30 秒的 16 位 PCM。目标采样率 8–48 kHz，且不得低于任一素材。先把源区间量化到源采样帧，随后以线性插值转换到目标时基；转换阶段取整到 int16，按片段列表顺序乘轨道增益累加，再应用主增益和最终饱和。静音片段保留时间轴占位。时间换算使用 Python `round`。

编排结果可超过 30 秒；这类结果可下载，但不能直接送入仍限制 30 秒输入的旧 `/inspect`、`/clip`、`/mix` 或素材导入。

Python 调用可通过 `Workbench(path)` 的 `assets`、`projects`、`renders` 使用同一业务能力，退出上下文会关闭队列并释放工作区。底层库供持有该独占工作区的进程使用，不支持多进程共享调度。

## 自动化验证与容器

```sh
python -m unittest discover -v
docker compose up --build -d
```

`tests/test_pcm_compatibility.py` 经真实 HTTP 路径验证 `/clip`、`/mix` 的输出可再次作为 `/inspect`、`/clip`、`/mix` 的输入：逐样本比较解码后的 16 位整数、核对 WAV 头部事实与 `/inspect` 返回值一致，并覆盖 30 秒上限素材、二次处理与并发请求的输入对应关系。断言为精确相等，不设容差，也不缩短合法素材。

容器使用非 root 用户和持久卷，端口只绑定本机。停止信号由 Python 服务接收并关闭 PCM/FFmpeg 工作线程。当前不支持滚动共享同一工作区，也没有音频效果、降采样或分布式队列。

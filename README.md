# GSVI 适配器 for GPT-SoVITS

将 GPT-SoVITS 的语音合成能力桥接为 AstrBot 可用的 GSVI TTS 提供商，让 QQ/微信等平台的机器人能用你训练的声音说话。

**角色全部在网页上管理**：新增、编辑、删除、切换、试听，不用碰任何代码。每个角色可以绑定自己的一对 GPT/SoVITS 权重，切换角色时自动热切换模型。

---

## 下载

不想看源码？直接去 [**Releases**](https://github.com/xiaoshunserendipity/gsvi-adapter-gpt-sovits/releases) 下载 `GSVI-Adapter-v3.0.0.exe`。

> 下载后可以随意改名（比如改回 `启动GSVI适配器.exe`），程序不依赖自己的文件名。
>
> 这个 exe 只是**启动器**，仍然需要你自己装好 GPT-SoVITS——它靠 GPT-SoVITS 自带的 `runtime\python.exe` 运行，本身不含推理环境。

---

## 来源与致谢

本项目基于 B 站 UP 主 **暮雨沐雨了** 的原始适配器代码修改而来：

| | |
|---|---|
| 视频 | [连接至 astrbot 模型提供商的本地部署 gpt-sovits](https://www.bilibili.com/video/BV1d8gm6WEpj) |
| 短链 | <https://b23.tv/jBAmuzM> |
| UP 主主页 | <https://space.bilibili.com/691266634> |

原版是一个约 160 行的最小可用实现：一个 FastAPI 服务，把 GSVI 协议的 `/infer_single` 翻译成 `TTS.run()` 调用，角色信息写死在代码里的 `GSVI_CHARACTERS` 字典中。它解决了「AstrBot 内置的 GPT-SoVITS 提供商需要额外配置服务、不能开箱即用」的问题，思路很直接。

本仓库在它的基础上做了以下修改：

| 方向 | 改动 |
|---|---|
| 角色管理 | 配置从代码里搬到 `gsvi_characters.json`，提供 `/ui` 网页增删改 + 目录浏览器 + 试听 |
| 多音色 | 每个角色可绑定自己的 GPT/SoVITS 权重，请求时自动热切换，失败自动回滚 |
| 协议健壮性 | 处理 AstrBot 把 `api_base` 填进 `dl_url` 的问题；`dl_url` 失败回退到角色自带参考音频 |
| 语言处理 | 区分「参考音频语言」与「待合成文本语言」，试听文本自动检测语言 |
| 工程化 | 可选 API Key、目录白名单、音频缓存 TTL 清理、路径穿越防护、启动自检、分发包制作 |
| 兼容性 | 适配 GPT-SoVITS runtime 的 Python 3.9；启动器 exe 一键启动 |

原版的思路和协议对接部分保留了下来，这部分是它的价值所在。

---

## 前置条件

1. **GPT-SoVITS 已安装并能正常使用**（`webui.py` 可以启动推理）
2. **已完成模型训练**，每个角色手上有两个权重文件和一个参考音频：
   - 一个 `.ckpt` 文件（GPT 阶段权重）
   - 一个 `.pth` 文件（SoVITS 阶段权重）
   - 一段 3~10 秒的参考音频（`.wav`）

如果还没训练，请先通过 GPT-SoVITS 的 WebUI 完成训练流程。

---

## 文件说明

| 文件 | 说明 |
|---|---|
| `gsvi_adapter.py` | 适配器主程序，桥接 GPT-SoVITS 和 AstrBot |
| `启动GSVI适配器.exe` | 启动器，**双击这个**。不在仓库里，去 [Releases](https://github.com/xiaoshunserendipity/gsvi-adapter-gpt-sovits/releases) 下载 `GSVI-Adapter-v3.0.0.exe`（可改名） |
| `启动GSVI适配器.bat` | 备用启动脚本，没有 exe 时用它 |
| `gsvi_launcher.py` | 启动器源码。打 tag 时 GitHub Actions 会自动构建 exe 并挂到 Release |
| `gsvi_characters.json` | **角色配置**，首次启动自动生成，之后由网页读写 |
| `制作分发包.bat` | 双击它，把适配器 + 音色打成 zip 发给别人 |
| `制作分发包.py` | 上面那个 bat 实际执行的脚本 |

---

## 使用步骤

### 第一步：放置文件

把 `gsvi_adapter.py` 和 `启动GSVI适配器.exe` 复制到 GPT-SoVITS 项目根目录下（跟 `api_v2.py`、`runtime\`、`GPT_SoVITS\` 同级）。

```
GPT-SoVITS/
├── api_v2.py
├── runtime/
├── GPT_SoVITS/
├── GPT_weights_v2Pro/          ← 你的训练输出
├── SoVITS_weights_v2Pro/       ← 你的训练输出
├── gsvi_adapter.py             ← 新文件
├── 启动GSVI适配器.exe           ← 从 Releases 下载，双击这个
├── gsvi_characters.json        ← 首次启动自动生成
└── gsvi_audio/                 ← 自动创建，缓存生成的音频
```

### 第二步：配置推理权重（tts_infer.yaml）

打开 `GPT_SoVITS\configs\tts_infer.yaml`，找到最顶上的 **`custom`** 段：

```yaml
custom:
  device: cuda                          # 有 N 卡填 cuda，否则 cpu
  is_half: true                         # N 卡且显存 ≥6G 填 true，cpu 填 false
  version: v2Pro                        # 训练时选的模型版本
  t2s_weights_path: GPT_weights_v2Pro/爱莉希雅-1-e15.ckpt
  vits_weights_path: SoVITS_weights_v2Pro/爱莉希雅-1_e8_s344.pth
  bert_base_path: GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large
  cnhuhbert_base_path: GPT_SoVITS/pretrained_models/chinese-hubert-base
```

> 这里的权重只是**启动时的初始模型**。之后在网页上给角色绑定权重，适配器会自动切换过去。填你最常用的那个即可。

**路径注意事项：**
- 用**相对路径**（从项目根目录开始写），不要写 `D:\xxx\xxx` 绝对路径
- **不要用双引号包裹路径**——YAML 双引号里的 `\` 会被当成转义符导致 `unknown escape character` 报错
- 如果路径含中文，编辑器可能显示为 `爱莉` 这种形式，属于 YAML 自动转码，**不影响使用**
- 确实需要绝对路径的话，用**单引号**：`'D:\xxx\xxx.ckpt'`

| 字段 | 含义 |
|---|---|
| `t2s_weights_path` | GPT（文本→语义）阶段权重路径 |
| `vits_weights_path` | SoVITS（语义→语音）阶段权重路径 |
| `version` | 模型架构版本，训练时选的哪个就填哪个 |
| `device` | `cuda`（显卡推理）或 `cpu` |
| `is_half` | 半精度加速，cuda 建议开，cpu 必须关 |
| `bert_base_path` | BERT 模型路径，**保持默认不改** |
| `cnhuhbert_base_path` | CNHubert 模型路径，**保持默认不改** |

### 第三步：启动适配器

双击下载的 exe（Release 里叫 `GSVI-Adapter-v3.0.0.exe`；没下 exe 就用备用的 `启动GSVI适配器.bat`），看到以下输出即为成功：

```
============================================
  GSVI Adapter for GPT-SoVITS
  工作目录: D:\GPT-SoVITS
============================================
  启动命令: ...\runtime\python.exe ...\gsvi_adapter.py -a 127.0.0.1 -p 8000
  关闭本窗口即停止服务。
```

- 首次启动会生成 `gsvi_characters.json`，并打印 `[config] 首次运行，已生成配置文件`
- `启动GSVI适配器.exe` 只是个**启动器**（约 8MB），它负责找到 `runtime\python.exe` 并用它跑 `gsvi_adapter.py`。改配置只改网页，**不需要重新打包**
- 这个窗口**必须一直开着**，关了 AstrBot 就连不上了

### 第四步：在网页上添加角色

浏览器打开 <http://127.0.0.1:8000/ui>，点左上角「**+ 新增角色**」：

| 表单项 | 填什么 |
|---|---|
| 角色名 | 例如 `爱莉希雅`，**AstrBot 里「角色」栏要填这个名字**，严格区分大小写 |
| 参考音频 | 点「浏览…」选一段 3~10 秒的 wav |
| 参考音频文本 | 这段音频里说的**原话**，必须完全一致，否则音色会飘 |
| 参考音频语言 | 中文 / 英文 / 日文 / 韩文 / 粤语 / 自动 |
| GPT 权重 (.ckpt) | 点「浏览…」选你的 `.ckpt`；留空则沿用当前模型 |
| SoVITS 权重 (.pth) | 点「浏览…」选你的 `.pth`；必须和上面成对 |
| seed | `-1` 随机；填固定整数则每次合成都一样 |
| 并行推理 | 开着更快但吃显存，显存紧张就关掉 |

保存后卡片会立刻出现，点「**试听**」当场听效果——调参考文本和语言的时候，这个比反复去 QQ 里试快得多。

> 权重路径**要么都填、要么都留空**。只填一个会保存失败，因为跨版本的 GPT+SoVITS 组合会出杂音甚至崩溃。

### 第五步：在 AstrBot 中新增 TTS 提供商

打开 AstrBot WebUI → 配置 → 服务提供商 → 新增：

| 配置项 | 填写内容 |
|---|---|
| 提供商类型 | **GSVI TTS API** |
| API 地址 | `http://127.0.0.1:8000` |
| API Key | 留空（除非你启动时加了 `-k`） |
| 模型版本 | 与 `tts_infer.yaml` 的 `version` 一致，如 `v2Pro` |
| 角色 | 网页上创建的角色名 |
| 文本语言 | `中文`（角色配了 `prompt_lang` 的话会被它覆盖） |
| 情感 | `默认` |

点击 **测试**，返回"合成成功"即可。

### 第六步：在平台适配器中启用 TTS

在对应平台（QQ / 微信等）的适配器设置中：

- TTS 提供商 → 选择刚才创建的 `gsvi_tts`
- 开启 TTS

> **注意**：平台级 TTS 开启后机器人的回复**只发语音，不发文字**。如需同时发送文字和语音，请使用 PrivateCompanion 插件的 TTS 功能（调整触发概率至 1.0 即可），而不是平台适配器的 TTS。

---

## 管理界面

打开 <http://127.0.0.1:8000/ui>。所有改动**即时生效，不用重启**。

### 角色卡片

每张卡片显示角色名、绑定的权重、参考音频、语言、seed，右上角标签表示状态：

| 标签 | 含义 |
|---|---|
| 已加载 | 这个角色绑定的权重正是当前显存里的模型 |
| 可切换 | 绑了权重，但当前加载的是别的模型，点「切换」会热切换过去 |
| 沿用当前 | 没绑权重，直接用 `tts_infer.yaml` 里加载的模型 |
| 配置有问题 | 参考音频或权重文件找不到了，卡片下方会写明具体原因 |

四个操作按钮：**试听** / **切换** / **编辑** / **删除**。

### 目录浏览器

点路径框旁的「浏览…」会弹出文件选择器，可以直接点选 `.ckpt` / `.pth` / 音频文件，不用手打路径。

**能浏览的范围**：
- GPT-SoVITS 项目根目录（始终允许）
- 所有已配置文件的所在目录（自动放行，避免"要选文件却进不去那个目录"的死循环）
- 「设置」里手动添加的额外目录

如果某个目录进不去，会提示你把它加到白名单。例如参考音频放在桌面：

```
设置 → 目录浏览器额外允许的目录 → 加一行
C:\Users\你的用户名\Desktop\参考音频
```

### 设置

| 项 | 说明 |
|---|---|
| 试听文本 | 点「试听」时合成的句子，`{name}` 会替换成角色名 |
| 试听文本语言 | 默认「自动检测」（按字符判断中日韩英）。**写日文就选日文**——语言和文本对不上会合成出静音 |
| 目录浏览器额外允许的目录 | 每行一个绝对路径 |

> **为什么试听文本语言要单独设？** 因为「参考音频的语言」和「要合成的文本语言」是两件事。
> 你的角色参考音频是日文，不代表你要合成的句子也是日文。
> 选错了，文本前端（G2P）处理不了这些字符，就会产出一段 0.3 秒的底噪，听起来像"没声音"。
> 适配器现在会检测这种情况：合成结果过短且几乎无声时，界面会直接弹提示。

---

## 多角色与模型切换

### 自动切换（推荐）

AstrBot 请求里的「角色」字段就是 `model_name`。适配器收到请求后会比对：

- 该角色绑定的权重 == 当前已加载的 → 直接推理
- 不一样 → 先把权重热切换过去，再推理

所以你只需要在 AstrBot 里换角色，声音就跟着换，**不用重启适配器**。

切换耗时 1~3 秒（读盘 + 上显存）。当前设计**只保留一个模型在显存里**，两个角色来回高频对话时每次都要重载。

### 手动切换

网页上点「切换」按钮，或者：

```bash
curl -X POST "http://127.0.0.1:8000/switch?name=爱莉希雅"
```

### 切换失败会怎样

- 权重文件不存在 → 直接报错，当前模型不受影响
- 权重损坏 / 版本不匹配 / pth 内嵌的 BERT 路径在本机不存在 → 报错并**自动回滚到上一个可用模型**，服务不会挂
- 回滚信息会打印在启动窗口里，网页上会弹提示

---

## 分发给别人

前提：**对方已经装好 GPT-SoVITS，并且版本和你一致**。

### 怎么做

1. 在网页上把角色都建好、试听确认没问题
2. 双击 `制作分发包.bat`
3. 得到 `GSVI适配器-分发包.zip`

脚本会读取 `gsvi_characters.json`，把所有角色的权重和参考音频收进包里，并生成两份说明：`使用说明.txt`（操作步骤）和 `音色清单.txt`（每个角色的参考文本、语言、对应文件）。

### 包里有什么

```
gsvi_adapter.py / 启动GSVI适配器.exe / 启动GSVI适配器.bat / README.md
weights\        各角色的 .ckpt 与 .pth
refs\           各角色的参考音频
使用说明.txt     给对方看的步骤
音色清单.txt     重建角色所需的全部信息
```

**不含** `gsvi_characters.json` —— 那个文件里全是本机的绝对路径，发出去也用不了。对方按「音色清单」在网页上重建即可。

### 对方怎么用

1. 把 `gsvi_adapter.py`、`启动GSVI适配器.exe`、`启动GSVI适配器.bat` 放进他自己的 GPT-SoVITS 根目录
2. 双击 exe
3. 打开 <http://127.0.0.1:8000/ui>，照着「音色清单」新建角色
4. 试听确认

### 三个必须对齐的点

| 点 | 说明 |
|---|---|
| **版本必须同代** | v2Pro 的权重不能用在 v2/v3 上。对方 `tts_infer.yaml` 的 `version` 要和权重一致 |
| **权重必须成对** | `.ckpt` 和 `.pth` 来自同一次训练，不能和别的角色混搭 |
| **参考文本必须一致** | 一字不差，否则音色会飘 |

适配器会在启动自检里帮你查这个：从权重所在目录名（`GPT_weights_v2Pro` 之类）猜出版本，和 `tts_infer.yaml` 的 `version` 对不上就标 `!!`。切换失败时也会读权重内嵌的版本信息，直接告诉你是哪一代不一致。

> **传输提示**：别用邮件发 exe，多数邮箱直接拦。走网盘或压缩包。

---

## 配置文件

`gsvi_characters.json` 由网页读写，**不建议手改**（反斜杠转义容易出错）。格式如下，仅供参考：

```json
{
  "characters": {
    "爱莉希雅": {
      "ref_audio_path": "C:\\参考音频\\爱莉希雅.wav",
      "prompt_text": "放心了，我会好好加油的……",
      "prompt_lang": "zh",
      "gpt_weights": "D:\\GPT-SoVITS\\GPT_weights_v2Pro\\爱莉希雅-1-e15.ckpt",
      "sovits_weights": "D:\\GPT-SoVITS\\SoVITS_weights_v2Pro\\爱莉希雅-1_e8_s344.pth",
      "seed": -1,
      "parallel_infer": true
    }
  },
  "settings": {
    "allow_dirs": ["C:\\Users\\你的用户名\\Desktop\\参考音频"],
    "preview_text": "你好，我是{name}，很高兴见到你。"
  }
}
```

- 保存时先写 `.tmp` 再原子替换，断电或并发都不会写坏
- 文件损坏时会打印警告并用空配置启动，不会导致服务起不来
- 想备份/迁移，直接拷这个文件即可

---

## 接口列表

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/infer_single` | 需 Key | GSVI 推理接口，AstrBot 调用 |
| GET | `/audio/{filename}` | 无 | 下载合成好的 wav |
| GET | `/ui` | 无 | 管理界面 |
| GET | `/api/characters` | 无 | 角色列表 + 设置 |
| POST | `/api/characters` | 需 Key | 新增角色 |
| PUT | `/api/characters/{name}` | 需 Key | 编辑角色（含改名） |
| DELETE | `/api/characters/{name}` | 需 Key | 删除角色 |
| GET | `/api/settings` | 无 | 读取设置 |
| PUT | `/api/settings` | 需 Key | 保存设置 |
| GET | `/api/browse` | 需 Key | 目录浏览 |
| POST | `/api/preview` | 需 Key | 试听合成 |
| POST | `/switch` | 需 Key | 手动切换角色 |
| GET | `/` | 无 | 服务状态 |
| GET | `/health` | 无 | 健康检查 |

> 「需 Key」的意思是：设了 `-k` 就必须带；没设 `-k` 时只允许本机（127.0.0.1）访问，局域网来的请求会被拒。

---

## 命令行参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-a` | 绑定 IP 地址 | `127.0.0.1`（仅本机访问） |
| `-p` | 绑定端口 | `8000` |
| `-c` | tts_infer.yaml 路径 | `GPT_SoVITS/configs/tts_infer.yaml` |
| `-k` | API Key，设置后写接口和浏览接口需携带 | 空（仅本机可写） |
| `--audio-ttl` | 音频缓存保留秒数 | `600` |
| `--audio-max-files` | 音频缓存最多文件数 | `300` |

```bash
# 局域网内其他设备也能访问（必须加 -k，否则改不了配置）
python gsvi_adapter.py -a 0.0.0.0 -p 8000 -k 你的密钥

# 自定义端口
python gsvi_adapter.py -p 9000

# 指定配置文件
python gsvi_adapter.py -c my_tts_config.yaml
```

---

## 常见问题

| 问题 | 原因 | 解决方法 |
|---|---|---|
| 端口被占用 (`10048`) | 旧的适配器进程没关 | `netstat -ano \| findstr 8000` 查进程号，`taskkill /f /pid 进程号` 杀掉 |
| AstrBot 报"未找到角色" | 网页上的角色名和 AstrBot 里填的不一致 | 错误信息里会列出所有可用角色名，照着改（严格区分大小写） |
| 网页上改了角色名，机器人还是旧声音 | AstrBot 那边还填着旧名字 | 改名后要去 AstrBot 的「角色」栏同步修改 |
| 目录浏览进不去某个文件夹 | 不在允许范围内 | 「设置」→ 把该目录加进白名单 |
| 网页点保存提示 403 | 服务绑了 `0.0.0.0` 但没设 Key | 启动时加 `-k 密钥`，并在网页右上角填同一个值 |
| 保存提示"文件不存在" | 权重/参考音频被移动或删除了 | 重新用「浏览…」选一次 |
| 切换模型报"内嵌的预训练路径不存在" | 该 pth 是在别的机器上训的，里面记的 BERT 路径在你本机没有 | 把 `chinese-roberta-wwm-ext-large` / `chinese-hubert-base` 放到报错提示的路径下 |
| 切换后音色不对/出杂音 | GPT 和 SoVITS 权重不是同一批训练产出 | 确认两个文件来自同一次训练，且 `version` 一致 |
| 自检显示"GPT 权重是 v3，yaml 里是 v2Pro" | 权重版本和配置文件对不上 | 改 `tts_infer.yaml` 的 `version`，或换用同代权重 |
| 分发包在对方机器上加载失败 | 对方的 GPT-SoVITS 和你的不是同一代 | 只能同代使用；不同代只能让对方自己训练或找对应版本权重 |
| 换角色时明显卡顿 | 每次切换都要重新读权重上显存 | 正常现象（1~3 秒）。高频交替场景可考虑只用一个角色 |
| 参考音频是日文但读成中文 | 角色语言配成了中文 | 编辑角色，把「参考音频语言」改成日文 |
| `unicodeescape` 报错 | 手改 json 时路径没转义 | 别手改 json，用网页改；或把 `\` 全写成 `\\` |
| `unknown escape character` | YAML 双引号里写了反斜杠路径 | 改用相对路径、去掉引号、或改成单引号 |
| exe 被杀毒软件拦下/报毒 | PyInstaller 单文件程序的常见误报 | 加白名单，或直接用 `启动GSVI适配器.bat` |
| 双击 exe 一闪而过 | 缺文件，程序会停窗显示原因 | 看窗口里的 `[错误]` 提示 |
| 开启后只发语音没有文字 | 平台级 TTS 替代了文字消息 | 改用 PrivateCompanion 插件的 TTS 功能 |

---

## 原理

```
AstrBot (GSVI 提供商)
    │
    │  POST /infer_single  {text, model_name, ...}
    ▼
GSVI 适配器 (端口 8000)          ← 本项目 gsvi_adapter.py
    │
    │  ① 查角色配置 → 参考音频 + 绑定的权重
    │  ② 权重与当前不同？→ 热切换 GPT/SoVITS（失败自动回滚）
    │  ③ 拼接参数 → 调用 TTS.run()
    ▼
GPT-SoVITS 推理引擎 (TTS.run)
    │
    │  推理完成 → 返回音频数据 (sr, numpy array)
    ▼
GSVI 适配器
    │
    │  存为 wav 文件 → 返回 JSON {msg, audio_url}
    ▼
AstrBot → GET /audio/xxx.wav → 下载音频 → 发送到聊天平台
```

GPT-SoVITS 原生 API（端口 9880, 端点 `/tts`）直接返回音频流，接口格式与 AstrBot 内置的 GSVI 提供商不兼容。本适配器在中间完成协议转换——无需修改 GPT-SoVITS 或 AstrBot 的任何代码。

适配器**没有**走 GPT-SoVITS 的 9880 API，而是在自己的进程里直接 `import` 推理引擎并加载权重。所以运行适配器时不需要另外启动 `api_v2.py` 或 WebUI。

---

## 许可

原始代码版权归 UP 主 **暮雨沐雨了** 所有，来源见上方「来源与致谢」。

本仓库的修改部分可自由使用、修改、分发。二次分发时请保留来源说明与视频链接。

> 原视频未声明具体开源协议，因此本仓库暂未附加 LICENSE 文件。如果原作者后续明确了协议，请以其为准。

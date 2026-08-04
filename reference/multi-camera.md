# Multi-camera / multi-source alignment

適用：一堂課有 ==一份（或數份）長錄音== ＋ ==多支手機拍的片段影片／照片==，素材可能
來自 ==兩三個不同的人==。目標＝每支影片掛到正確的課程段落、算出實作段落的覆蓋率、
產出可點段落跳秒的網頁。

以下每一條都是在一場為期兩天、雙人共 66 個檔案／5 份錄音／117 段的工作坊
（2026-07，以下稱 Workshop-X）上付過代價換來的。第一次跑通時，光是「錄音起點靠
口述線索估」這一項就讓整批影片掛錯段落。

## Contents

- [Canonical entry — the alignment backbone](#entry)
- [硬規則](#hard-rules)
- [流程](#flow)
- [多來源去重](#dedup)
- [轉檔與檔名](#transcode)
- [網頁](#web)
- [近場音軌的真正價值](#near-field)

---

## Canonical entry — the alignment backbone {#entry}

==Start here.== 以前這一段是靠人工推估起點再手動核對；現在的正式入口是
`media_capture_index.py --emit-alignment`。

```bash
# 1) 建索引 ＋ 寫出對齊假設
python <skill-dir>/scripts/media_capture_index.py index.json 我=<DIR1> 他人=<DIR2> \
    --emit-alignment alignment.json

# 2) 讀 alignment.json：reliable=false 的來源沒有可信起點，不可拿來對齊
# 3) 每個來源轉錄後，量測真實偏移（指令由步驟 1 直接印出來）
python <skill-dir>/scripts/xcorr_media_offsets.py xcorr.json \
    --clip-asr <clip ASR 根目錄> --recording A=<該錄音的 ASR 目錄> --index index.json

# 4) 把量測結果餵回來比對
python <skill-dir>/scripts/media_capture_index.py index.json <DIRS…> \
    --emit-alignment alignment.json --xcorr-results xcorr.json
```

==核心契約：拍攝時間是「假設」，逐字稿交叉相關是「證據」。==

`alignment.json` 每個來源一列：`capture_start`、`start_source`
（`exif`/`ffprobe`/`mtime`/`none`）、`duration_s`、`reliable`、`conflict`。
`reliable=false` 代表起點來自 mtime 或根本沒有——==這種起點絕不可用來對齊素材==，
它被記錄下來只是為了讓人看見缺口，不是為了讓腳本拿去用。

`--xcorr-results` 會把「宣稱的偏移」與「量測的偏移」對照，差距超過 5 秒就在兩個來源
上標 `"conflict": true` 並大聲警告。==任何情況都不會自動修正==：衝突代表其中一個時鐘
在說謊，哪一個是人的判斷。衝突是 warning 不是 error，程式仍以 0 結束。

`--emit-alignment` 只會列印 xcorr 指令、不會自己跑——交叉相關要求每個來源都先轉錄
過，那是 GPU/CPU 成本，什麼時候花由使用者決定。

## 硬規則 {#hard-rules}

1. ==拍攝時間只認媒體內部的 metadata，永遠不用檔案 mtime==
   - 影片：`com.apple.quicktime.creationdate`（自帶 +08:00）＞ `creation_time`
     （UTC，要加時區）
   - ==AVCHD `.MTS`：容器裡什麼時戳都沒有==，ffprobe 一片空白。時鐘在串流內的
     **MDPM** 封包裡，用 `scripts/_mdpm.py` 讀（`media_capture_index.py` 已自動
     採用）。沒有這一步，研討會素材（幾乎清一色 AVCHD）會整批被判定為「無拍攝
     時間」，整個對齊層直接失效——2024-05 Conference-Y 20 支 MTS 就是這樣被漏掉的
   - 照片：EXIF `DateTimeOriginal`
   - 錄音筆：==檔名內嵌的時戳（`240526_1119`）算裝置聲明，可用==；秒數可用 mtime
     補足，==但僅限 mtime 落在檔名同一分鐘內==（該批 5/5 支吻合＝錄音筆自己寫檔，
     不是複製產物）。分鐘對不上就只採檔名
   - mtime 會被 zip／雲端同步／相簿匯出改寫成上傳時間。Workshop-X 那批 mtime 全數
     錯掉數小時，而每一個 QuickTime creationdate 都精確到秒
2. ==時間戳是「開始錄影」的時間，不是結束==（==但 AVCHD 的 **mtime** 是結束==：
   MDPM 起始＋長度＝mtime，2024-05 那批 20 支誤差 0.2–3.2 秒。把 mtime 當起點會
   整整錯掉一支片的長度，30 分鐘的片就錯 30 分鐘）
   - 實測：每份錄音用 4–13 支影片各自回推錄音起點，==「當開始」離散 3–7 秒；
     「當結束」離散 268–843 秒==（clip 長度 27–963 秒）。同一錄音的所有 clip 必須
     回推出同一個起點，所以結論唯一
   - ==要重驗這件事就用這個測試==：兩種假設各算一次離散度，長度變異大的 clip 群體
     會把錯的假設放大
3. ==先建索引，再轉檔——轉檔會把拍攝時間清光==
   - 實測：NVENC 轉出來的 mp4 ==連 `creation_time` 都沒有==（不是變成轉檔時間，是
     整個不見）⟹ 先轉檔再想對齊，原始時間永遠回不來
   - 想讓成品保有時間：轉檔後補寫
     `ffmpeg -i out.mp4 -c copy -metadata creation_time="2026-07-26T07:12:30.000000Z" out2.mp4`
     （實測可寫入；UTC 格式）
   - 同理，==抽近場音軌也要在刪原始檔之前==
4. ==錄音的時鐘錨點必須「測」，不可用口述線索猜==
   - 舊估法：靠「休息 10 分鐘」「1 點 10 分回來」推出的起點，其中一份 ==差 44 分
     鐘==，導致每支影片都掛錯段落，還交付過一份錯的缺口清單
   - 正解＝`xcorr_media_offsets.py`：把每支影片的**近場逐字稿**與該錄音的**遠場逐
     字稿**做 6-gram 交叉相關，共享 gram 對 (遠場時間 − clip 時間) 投票，取眾數＝
     該 clip 在錄音內的真實秒數；再用拍攝時鐘回推錄音起點
   - ==驗收指標＝同一錄音各 clip 回推起點的離散度==（≤30 秒才算成立），==外加一支
     影片的內容對照==（段落標題與畫面／台詞要吻合）
5. ==機器對齊全綠 ≠ 對==：內容對照才是最後一關。Workshop-X 修正前後的分水嶺就是
   「某支影片的內容明明是足部掃描教學，不可能是分組實作」
6. ==clip 逐字稿目錄名要對得回媒體檔==：`xcorr_media_offsets.py` 靠這個把「錄音內秒
   數」換成「錄音起點時鐘」。目錄名＝媒體檔 stem 最省事；已經改成友善名就給
   `--name-map {"asr目錄名":"媒體檔名"}`。工具會印 ⚠️ 並列出對不到的 clip，不會靜
   默算不出來——這個保護是實測踩到才補的
7. ==低信心結果會寫進 JSON 並標記==，不只印在 console。這正是它存在要防的 44 分鐘
   錯位場景

## 每個裝置各有一個時鐘，而且都是錯的 {#device-clock-calibration}

拍攝時間讀出來只解決了一半。==每台裝置的時鐘各自漂移，而且沒有任何一台會告訴你它
錯了。== 2024-05 Conference-Y實測，三台裝置三種錯法：

| 裝置 | 它宣稱的 | 實際誤差 | 怎麼量出來的 |
|---|---|---|---|
| AVCHD 攝影機 | MDPM 時戳 | ==快 1 天又 5 分 30 秒== | 交叉相關 |
| 錄音筆 | 檔名 `240526_1119` | 準（＝本課程的時間錨點） | 與各場次、各休息空檔吻合 |
| Canon G7X II | EXIF | ==快 72 分 30 秒== | 照片 OCR 對投影片 |

日期錯一天特別惡毒：它讓 Day1 素材看起來像 Day2，而**時分秒仍然是對的**，所以每一
項單看都合理。只有把多台裝置擺在一起才會露餡。

**校準流程**：

1. ==先選錨點==。挑一台有外部佐證的裝置（本例：錄音筆的每一段起訖都落進官方議程的
   場次與休息空檔）。==錨點是判斷，不是計算==，要寫下理由。
2. **有聲音的來源 → 交叉相關**（`xcorr_media_offsets.py`）。把要校的來源當 clip、
   錨點當 recording，測出它在錨點錄音裡的真實秒數，回推時鐘差。
   驗收＝同一裝置多支素材回推出的偏移要一致；本例 3 支影片得 5:31／5:30／5:28，
   離散 ±3 秒 ⟹ 成立。
3. **照片（沒有聲音，無法交叉相關）→ OCR 對投影片**。照片本來就是拍投影片：對每張
   照片跑 RapidOCR，跟 `slides_grounded.json` 裡該投影片的 `quick_text` 算字元
   3-gram Jaccard，取最佳匹配的投影片顯示起始時刻，`偏移 = 照片EXIF − 投影片時刻`，
   取中位數。本例 31/32 張匹配成立，中位 4350 秒、標準差 30 秒。
   ==只在同時有影片與照片的那一場做校準==，其他場次留著當驗收。
4. ==用沒參與校準的素材驗收==。本例把 72 分 30 秒套到另外四場（完全沒參與計算）
   的照片上，289 張有 288 張精準落進其場次的錄音窗口，唯一例外是開錄前 45 秒拍的
   標題投影片。==這一步才是校準成立的證據；用校準來源自己驗自己不算。==
5. 套用時走 `--clock-offset '<selector>=<秒>'`（`media_capture_index.py` /
   `course_timeline.py`），裝置原本的宣稱會留在 `capture_raw_start`。
   ==絕不手改時戳==——被覆蓋掉的原始宣稱，之後沒人查得回來。

**精度的誠實表述**：校準殘差（本例 ±30 秒）與照片拍攝間隔（22–52 秒）同量級，所以
「照片對到哪一張投影片」可靠，「對到逐字稿哪一句」不可靠。==寫進筆記時按投影片切換
點對齊，不要宣稱句級精確。==

## 流程 {#flow}

| # | 做什麼 | 指令／產物 |
|---|---|---|
| 0 | 盤點素材、量磁碟 | zip 先在**大容量磁碟**解壓（30 GB zip → 30 GB 檔案）；解壓時逐檔比對 size 才算驗證過 |
| 1 | 建立拍攝時間索引＋對齊假設 | `media_capture_index.py index.json 我=<DIR1> 他人=<DIR2> --emit-alignment alignment.json` |
| 2 | 抽近場音軌（==在刪原始檔之前==） | `ffmpeg -i clip -vn -ac 1 -ar 16000 -c:a pcm_s16le clip.wav`，逐支比對 wav 長度＝影片長度 |
| 3 | 近場逐字稿 | 走本 skill 的轉錄流程（長檔用 chunked runner）。實測在一張消費級 GPU 上 ==2.35× 實時==（3.7 小時音檔 29 分鐘） |
| 4 | 測真實偏移 | `xcorr_media_offsets.py offsets.json --clip-asr <asr> --recording A=<rec_A> --index index.json` |
| 5 | 回推段落時鐘 | `measured_start + start_s`；把時間軸檔裡的估計起點換成實測值並留備份 |
| 6 | 落段＋覆蓋率 | 每 clip 對每段算時間重疊；段落覆蓋率＝該段被影片蓋到的秒數 ÷ 段長（多支影片要先 merge 區間再加總） |
| 7 | 去重（多來源） | 見下 |
| 8 | 轉檔歸檔 | 見下 |
| 9 | 產網頁 | 見下 |

`xcorr_media_offsets.py <out> --clip-asr DIR --recording NAME=DIR [--index PATH]
[--name-map JSON] [--n 6] [--bin 5] [--min-votes 40] [--max-gram-freq 4]`

`--n` 是 n-gram 長度（預設 6），`--bin` 是偏移直方圖的秒數分箱，`--min-votes` 是判
定成立所需的最低票數，`--max-gram-freq` 排除過度常見（因此無鑑別力）的 gram。
`--index` 接 `media_capture_index.py` 的輸出，才能把「錄音內秒數」換算成時鐘。

查詢近場內容：`query_near_field.py [day] [start] [end] --asr DIR --index PATH
[--map JSON] [--grep TERM] [--ctx 2]` — 兩種模式：給日期＋起迄依時鐘查，或
`--grep <詞>` 找關鍵詞。

## 多來源去重 {#dedup}

- 規則＝==使用者自己拍的優先==；別人拍的只在使用者沒拍到時保留
- 判定＝別人那支被使用者的影片**時間覆蓋 ≥70%** ⟹ 重複
- ==先移到 holding 資料夾、確認成品可播再刪==（Workshop-X：13 支 12.9 GB）
- 會遇到「別人那支其實就是你拍的」：長度、大小、`quicktime.model` 完全一致 ⟹ 他從
  你那裡轉存的，可以直接刪
- ==別人獨有的段落一定要留==：Workshop-X 第一天前半場兩小時全靠另一位參與者，使用者
  完全沒拍

## 轉檔與檔名 {#transcode}

- 壓縮：`hevc_nvenc -preset p5 -rc vbr -cq 30 -tag:v hvc1 -c:a aac -b:a 128k
  -movflags +faststart`（1080p30 手機素材 17 GB → 4.6 GB，10× 實時）
  - ==H.265 只在部分瀏覽器播得動（Firefox 不行）==；要給不特定對象看就改 H.264
    （同一套參數換 `h264_nvenc -cq 23`）。這與 `export_web.py --compress` 選 H.264
    的理由一致
  - 任何 GPU 批次都用你的 GPU 序列化機制包起來（本機用 `gpu_lease.py`；一般環境就
    一次跑一個）
- 檔名帶語意才找得到：`場次_時間_段落標題_拍攝者.mp4`
  - ==檔名嵌了段落標題，就等於把對齊結果寫死在檔名裡==——對齊一改就必須整批改名
    （Workshop-X 改了 28 支）。要嘛接受改名成本，要嘛檔名只放時間、標題留給網頁

## 網頁 {#web}

- 單檔 HTML、資料內嵌（`const DATA = {...}`）；影片走相對路徑放同名支援資料夾
- 三欄：影片＋近場逐字稿／筆記全文／段落列
- ==點段落 → `video.currentTime = 段落起點 − clip 起點`==（這個偏移才是整套對齊的
  實際價值）
- 段落 ↔ 筆記章節配對：先比標題（字元 3-gram 相似度 ≥0.34），再退回「段落標題＋摘要
  的關鍵詞 vs 章節全文命中數」。Workshop-X 得到 101/117；配不到的是分組實作／休息
  宣布這類本來就沒有章節的段落
- 近場逐字稿窗格：`timeupdate` 逐行跟隨、點行跳秒、屬於當前段落的行亮色
- 一定要附 ==缺口清單==（實作類段落覆蓋 <15%）給使用者去向其他人索取影片

課程型的段落網頁請改用 `export_web.py`（見 `segmented-mode.md` Step 9）；上面這套是
多機位素材專用的版型。

## 近場音軌的真正價值 {#near-field}

- ==不要指望它救回「分組實作」大段落==：那些時段通常沒人在拍。Workshop-X 六段分組
  實作，近場覆蓋 0–8%
- 它真正解掉的是 ==示範段落裡遠場聽不清的細節==——Workshop-X 靠近場結案了數個原本
  「不明」的操作前提（肌群數目、觸診骨標記、量測起始姿勢），並把幾段技術示範從
  「不明」推進到「明確」
- ==寫回筆記時引用格式＝影片檔名 ＋ 時鐘==，與遠場錄音的內部秒數引用區分開
  （Workshop-X 實績：第一天加 4 處、第二天加 25 處近場引用，並在待確認清單逐題標
  ✅／🟡）
- 近場稿的 ASR 錯字照 HARD RULE「只標不改」處理。實例：`too many 頭` ＝ too many
  toes、同音誤植的肌群名、`寬` ＝ 髖——這些靠上下文＋段落主題就辨得出來，改寫反而
  會洗掉線索

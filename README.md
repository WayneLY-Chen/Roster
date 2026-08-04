# 名單匠 Roster

蒐集**公開**的台灣 B2B 廠商聯絡資料、清理驗證、建檔管理、匯出報表，
並可透過你自己的 Gmail 寄送開發信。

Python 3.12+ ｜ 桌面應用程式（PySide6）｜ 本機 SQLite ｜ 也可用命令列操作

---

> ## ⚠ 使用條款與免責聲明
>
> **本工具僅供蒐集網站「公開顯示」的商業聯絡資訊。使用者須自行確認每一個爬取
> 目標的合法性，並自負全部責任。**
>
> **嚴禁**以本程式從事下列行為：
>
> 1. 爬取 `robots.txt` 明文禁止爬取之網站或路徑
> 2. 爬取網站使用條款或著作權聲明中禁止自動化蒐集之內容
> 3. 以任何方式繞過登入驗證、付費牆、CAPTCHA、速率限制或其他技術保護措施
> 4. 解析經 JavaScript、Cloudflare 或其他機制**刻意混淆**之聯絡資訊——
>    混淆本身即代表對方明示不希望被自動蒐集
> 5. 蒐集自然人之個人資料，或蒐集與商業往來無關之資訊
> 6. 以過高頻率請求而影響目標網站正常運作
> 7. 將蒐集所得資料轉售、公開散布，或用於騷擾、詐騙等不法用途
>
> 本程式預設遵守 `robots.txt`、插入請求延遲、並誠實表明 User-Agent。這些設定
> 可以調整，但**調整設定之行為及其後果，由使用者自行承擔**。
>
> 本程式係「依現況」提供，不附任何明示或默示之擔保。作者／提供者對於使用者
> 如何使用本程式**不具任何控制能力，亦不負任何監督義務**。使用者若以本程式
> 爬取不應爬取之網站、違反目標網站使用條款、違反《個人資料保護法》或其他法令，
> 其一切法律責任與後果（包含民事賠償、行政罰鍰及刑事責任）均由**使用者本人**
> 自行承擔，**作者／提供者一概不負責任**。
>
> 若不同意上述任何內容，請立即停止使用本程式並刪除之。
>
> 完整條文可在程式的「設定」頁查看，或見 [`core/legal.py`](core/legal.py)。

---

## 目錄

1. [五分鐘上手](#五分鐘上手)
2. [安裝](#安裝)
3. [每一頁在做什麼](#每一頁在做什麼)
4. [教學一：爬取一個網站](#教學一爬取一個網站)
5. [教學二：整理與匯出名單](#教學二整理與匯出名單)
6. [教學三：設定 Gmail 並寄送開發信](#教學三設定-gmail-並寄送開發信)
7. [教學四：設定自動排程](#教學四設定自動排程)
8. [命令列速查](#命令列速查)
9. [資料安全與上傳 git](#資料安全與上傳-git)
10. [法律與使用規範](#法律與使用規範)
11. [疑難排解](#疑難排解)
12. [給開發者](#給開發者)

---

## 五分鐘上手

1. 依下方[安裝](#安裝)步驟建好環境
2. 點兩下 **`start.bat`**
3. 到「**爬取**」頁 → 來源選 `sample` → 按「**開始爬取**」
4. 到「**公司**」頁，會看到 7 筆範例資料
5. 到「**匯出**」頁 → 格式選 Excel → 按「**匯出**」

`sample` 是內建的離線範例名錄，不會連網，用來確認整條流程正常。

---

## 安裝

需要 **Python 3.12 或更新版本**。在專案資料夾開啟終端機：

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

只有要爬「用 JavaScript 動態產生內容」的網站時，才需要多裝一次瀏覽器：

```bash
.venv\Scripts\python -m playwright install chromium
```

裝好之後，日常使用點兩下這兩個檔案就好：

| 檔案 | 用途 |
|---|---|
| `start.bat` | 開啟視窗介面 |
| `console.bat` | 開啟已啟用虛擬環境的命令列 |

### 搬動或改名資料夾之後

`start.bat` 與 `console.bat` 都以自己所在的位置推算路徑，搬到哪都能用。

但 Python 的虛擬環境**本身**不可搬移，這是 `venv` 模組的已知行為、不是本專案
的設定：`.venv\Scripts\activate.bat` 會把建立當下的絕對路徑寫死，`pip.exe`、
`pytest.exe` 這類啟動器也在檔案裡嵌入了絕對路徑，搬動後就失效（`.venv\Scripts\
python.exe` 本身不受影響，所以 `start.bat` 照常運作）。

因此本專案一律用 `python -m pip`、`python -m pytest` 這種寫法，而 `console.bat`
不呼叫 `activate.bat`、自己設定環境變數。真的搬過位置之後如果想回到完全乾淨的
狀態，重建虛擬環境即可：

```bash
rmdir /s /q .venv
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

---

## 每一頁在做什麼

| 頁面 | 用途 |
|---|---|
| **儀表板** | 總覽：公司總數、信箱數、本週新增、疑似重複、業務階段分佈、最近爬取紀錄 |
| **公司** | 主要工作區。搜尋、篩選、新增、編輯、刪除、找出重複並合併 |
| **聯絡人** | 跨公司搜尋所有具名聯絡人 |
| **爬取** | 執行爬取、貼網址建立自訂來源、重新驗證所有信箱 |
| **匯入** | 從 CSV / Excel / JSON 匯入既有名單 |
| **匯出** | 匯出 Excel / CSV / JSON，可挑欄位、下篩選條件 |
| **郵件** | 編輯信件樣板、產生收件名單、預覽、寄送 |
| **日誌** | 依分類查看執行紀錄（爬取／資料庫／匯出／介面／錯誤） |
| **設定** | Gmail 帳號設定、外觀、設定總覽、備份與還原 |

---

## 教學一：爬取一個網站

### 方法 A — 用精靈（推薦，不用寫任何設定）

1. 到「**爬取**」頁，按「**＋ 自訂網址…**」
2. 貼上目標名錄的網址，按「**分析網頁**」
3. 程式會自己看懂那一頁的結構，並告訴你：
   - 這一頁找到幾筆資料
   - 抓得到哪些欄位
   - 有沒有「下一頁」
4. 在「**預覽抓到的資料**」看看內容對不對
5. 填「來源名稱」→ 按「**儲存來源**」或「**儲存並立即爬取**」

**正常情況你只要做這五步。** 網頁怎麼解析、CSS 選擇器是什麼，都不用管。

儲存後這個來源會出現在「來源」下拉選單，之後可以重複使用，
也可以被排程自動執行。設定存在 `custom_sources.yaml`。

#### 抓到的東西不對的時候

打開精靈最下面的「**進階設定**」（平常收起來）。裡面可以逐欄調整程式要從網頁的
哪個位置取值，也可以新增或刪除欄位。自動偵測失敗時，這一區會自己展開。

有兩個設定值得知道：

- **「下一頁」連結的位置** — 留空就只爬第一頁
- **「最多進入幾個詳細頁」** — 很多名錄的清單頁只有公司名稱，信箱與電話要**點進去**
  才看得到。這個上限設得比公司總數小的話，後面的公司就不會有聯絡資料
  （預設 100，分析後會自動調成該頁的實際筆數）

> 找不到重複區塊時，通常是該頁用 JavaScript 產生內容。
> 把 `config.yaml` 的 `crawler.engine` 改成 `playwright` 再試一次。

### 抓到的公司沒有信箱？

名錄不一定會公開每一家的信箱。「爬取」頁有「**補抓信箱**」按鈕：程式會逐一連到
那些**有網址但沒信箱**的公司自己的網站，找公開刊登的聯絡信箱。

每個網站都會各自檢查一次它自己的 `robots.txt`，並遵守請求間隔，所以會花上幾分鐘
——按下去之前會先告訴你預估時間與公司家數。命令列版本是 `python main.py enrich`。

### 方法 B — 直接寫設定檔

在 `config.yaml` 的 `crawler.sources` 加一段：

```yaml
- name: "my_directory"
  type: "generic_html"
  enabled: true
  start_url: "https://example.com/list?page={page}"
  max_pages: 5
  list_selector: "div.company-card"
  pagination:
    type: "query"            # query | next_link | none
  fields:
    company_name: { selector: "h3.name" }
    email:        { selector: "a[href^='mailto:']", attr: "href" }
    phone:        { selector: ".tel" }
    website:      { selector: "a.site", attr: "href" }
    address:      { selector: ".addr" }
    industry:     { selector: ".industry" }
    tax_id:       { selector: ".tax-id" }
```

每個欄位還支援 `regex`（從值裡再擷取一段）、`multiple`、`separator`。

### 方法 C — 命令列一次性爬取

```bash
python main.py crawl --url https://example.com/companies
python main.py crawl --url https://example.com/companies --save-as 我的名錄
```

---

## 教學二：整理與匯出名單

### 資料是怎麼被清理的

抓進來的原始文字會依序經過四關：

1. **正規化** — 全形轉半形、`+886` 電話前綴、`台`/`臺` 統一、網址補上通訊協定並移除
   `utm_*` 追蹤參數、從 `mailto:` 與「姓名 &lt;信箱&gt;」形式解出乾淨信箱
2. **驗證** — 信箱語法、拋棄式信箱網域、電話位數、網址格式、
   統一編號檢核碼（含第 7 碼為 `7` 的特例）
3. **MX 查詢** — 確認該網域是否真的收信；每個網域只查一次並快取
4. **去重** — 詳見下方

### 去重是怎麼判斷的

每筆資料會算出一把帶前綴的識別鍵，強度由高到低：

| 形式 | 依據 | 信心 |
|---|---|---|
| `tax:12345678` | 統一編號 | 精確 |
| `mail:a@b.com` | 電子信箱 | 精確 |
| `np:名稱｜電話` | 名稱＋電話 | 高 |
| `nw:名稱｜網域` | 名稱＋網站 | 高 |
| `n:名稱` | 僅名稱 | 中 |

寫入時不只比對這把鍵，還會分別用統編、信箱、名稱＋電話、名稱＋網域去找既有資料
——因為同一家公司可能在第 1 頁只露出電話、第 2 頁才有統編，算出來的鍵並不一樣。

**合併時只補空欄位，不會覆寫你手動整理過的內容。**

漏網之魚可以在「公司」頁按「**尋找重複**」，逐組確認後合併。

### 匯出

「**匯出**」頁可以：
- 選格式（Excel / CSV / JSON）
- 下篩選條件（關鍵字、產業、業務階段、只要有信箱的、只要使用中的、筆數上限）
- 勾選要匯出哪些欄位
- 指定檔名，或留空自動命名到 `output/` 資料夾

Excel 會自動凍結標題列、加上篩選、依內容調整欄寬（中文字寬度有另外計算）。
CSV 預設用 `utf-8-sig`，否則繁體中文版 Excel 會把中文顯示成亂碼。

---

## 教學三：設定 Gmail 並寄送開發信

### 第一步：產生 Google 應用程式密碼

**不要**使用你的 Google 帳號密碼。

1. 到 Google 帳戶 → 安全性 → 開啟「**兩步驟驗證**」（沒開的話無法產生應用程式密碼）
2. 前往 <https://myaccount.google.com/apppasswords>
3. 建立一組新的應用程式密碼，複製那 16 碼

### 第二步：在程式裡設定

到「**設定**」頁 → 「**Gmail 帳號**」區塊：

1. 填入 Gmail 地址與剛剛的應用程式密碼
2. 按「**儲存到系統**」

密碼會存進 **Windows 認證管理員**（macOS 是 Keychain），
**不會**寫進專案資料夾裡的任何檔案。所以整個專案資料夾可以安全地上傳 git。

3. 按「**測試連線**」確認帳號可用

> 想單獨撤銷授權時，到 Google 帳戶的應用程式密碼頁面刪除那一組即可，
> 不影響你的主密碼。

### 第三步：開啟寄信功能

到「**郵件**」頁的「寄件狀態」區塊，把「**啟用郵件寄送**」打開。不用改設定檔，
也不用重開程式。

旁邊還有一個「**實際寄出**」開關：

- **關著**（預設）＝演練模式。按「開始寄送」也只會寫進資料庫並標記為演練，**不會真的寄**
- **打開**＝真的寄。切換時會跳出確認，因為寄出去的信收不回來

> 兩個開關存在 `user_settings.yaml`，不會動到你手改過、寫滿註解的 `config.yaml`。
> 其他寄送參數（每日上限、間隔秒數、不重複寄送天數）仍在 `config.yaml` 的 `mailer` 區段。

### 第四步：寫信件樣板

到「**郵件**」頁左側。主旨直接打，內文按「**放大編輯**」開一個大視窗來寫——
下方那個小框只有幾行高，長一點的信打到一半就看不到自己寫了什麼。

放大視窗裡可以：

| 功能 | 說明 |
|---|---|
| 粗體 / 斜體 / 底線 | 選取文字後按按鈕 |
| 大標題 / 小標題 | 整行套用 |
| 項目符號 | 整行套用，連續幾行會自動變成一個清單 |
| 連結 | 選取文字後貼上網址 |
| **插入圖片** | 選圖片檔即可，會自動複製一份到 `templates/mail/images/` |

有格式的信會以 **HTML** 寄出，並且**同時附一份純文字版**——不是每個信箱都顯示
HTML，而且純 HTML 的信比較容易被判定成垃圾郵件。圖片以 **CID 附件**內嵌，
因為 Gmail 會把 `data:` URI 的圖片擋掉，收件者只會看到破圖。

可用變數：`{company_name}`、`{contact_person}`、`{industry}`、`{email}`、
`{phone}`、`{website}`、`{city}`

打錯的變數（例如 `{compnay_name}`）會直接報錯，不會把 `{compnay_name}` 原樣寄出去。
公司名稱裡有 `&`、`<` 這種符號時會自動處理，不會弄壞信件排版。

### 第五步：產生名單並寄送

1. 在「郵件」頁右側設定篩選條件 → 按「**產生名單**」
2. 名單表會列出每一家公司，以及**會寄／被略過**與略過原因
3. 按「**預覽第一封**」確認實際內容（有格式的信會直接顯示排版後的樣子）
4. 先按「**演練（不寄出）**」跑一次，確認統計數字合理
5. 確認無誤後把「**實際寄出**」開關打開，再按「**開始寄送**」
6. 會跳出二次確認，寫明要寄給幾家、用哪個帳號、間隔幾秒

### 內建的保護機制

| 機制 | 說明 |
|---|---|
| **請勿聯絡名單** | 優先於一切，且沒有任何設定可以覆寫 |
| 每日上限 | 依當天實際寄出的封數計算 |
| 寄送間隔 | 每封之間強制等待，避免被 Gmail 判定為垃圾郵件來源 |
| 不重複寄送 | 同一家公司在設定天數內不會再收到 |
| 只寄給驗證過的信箱 | 可關閉，但預設開啟 |
| 退訂說明 | 強制附加在每封信末尾，並加上 `List-Unsubscribe` 標頭 |
| 預設演練模式 | 新環境第一次跑一定是演練，不會意外寄出 |
| 完整稽核紀錄 | 每封信在寄出**前**就寫入資料庫，中途當掉也留得下證據 |

有人回信要求退訂時，到該公司的詳細視窗勾選「請勿聯絡」，之後永遠不會再寄給他。

---

## 教學四：設定自動排程

編輯 `config.yaml`：

```yaml
scheduler:
  enabled: true
  mode: "daily"            # daily | hourly | interval
  at: "03:00"              # mode=daily 時的執行時間
  every_minutes: 360       # mode=interval 時的間隔
  sources: []              # 留空 = 所有已啟用的來源
  verify_after_crawl: true # 爬完自動重新驗證信箱
  catch_up: true           # 開機時若已錯過時間就補跑一次
```

**重要：這是桌面程式，沒有背景服務——排程只在程式開著的時候才會執行。**

想要不開視窗也能跑，用命令列版本：

```bash
python main.py schedule          # 前景執行，Ctrl+C 停止
python main.py schedule --status # 只看設定與下次執行時間
python main.py schedule --now    # 立刻執行一次
```

要開機自動執行，把 `python main.py schedule` 加進 Windows 工作排程器。

---

## 命令列速查

```bash
python main.py --help                          # 列出所有指令
python main.py check                           # 檢查設定是否正確
python main.py security                        # 上傳 git 前的安全檢查
python main.py encrypt                         # 查看資料庫加密狀態
python main.py encrypt --export-key            # 備份金鑰（換電腦前務必做）
python main.py encrypt --import-key            # 還原金鑰

python main.py crawl                           # 爬取所有已啟用來源
python main.py crawl -s sample                 # 爬取指定來源
python main.py crawl --url https://...         # 直接分析並爬取一個網址
python main.py crawl --list                    # 列出所有來源

python main.py verify                          # 重新正規化並驗證信箱
python main.py duplicates --merge              # 找出並合併重複資料

python main.py import 名單.xlsx                # 匯入試算表
python main.py export -f excel                 # 匯出 Excel
python main.py export -f csv --with-email      # 只匯出有信箱的
python main.py export --all                    # 三種格式都輸出

python main.py backup                          # 手動備份
python main.py backup --list
python main.py backup --restore crm-daily-20260803-030000.db

python main.py schedule                        # 執行排程
python main.py enrich                          # 到公司官網補抓公開信箱
python main.py gmail                           # 從收件匣擷取聯絡資訊
python main.py stats                           # 資料庫統計
python main.py gui                             # 開啟視窗
```

---

## 資料安全與上傳 git

### 上傳前先跑這個

```bash
python main.py security
```

它會檢查：憑證是否存在明文 `.env`、`.gitignore` 是否涵蓋所有敏感路徑、
有沒有敏感檔案已經被 git 追蹤、專案裡實際存在哪些敏感檔。
有問題會逐項列出並以錯誤狀態結束。

### 已經處理好的部分

- **資料庫裡的個資是加密的**（詳見下一節），拿到 `crm.db` 也讀不到信箱與電話
- **Gmail 應用程式密碼存在系統憑證保管庫**，專案資料夾裡沒有這筆資料
- `.gitignore` 涵蓋 `.env`、`data/`（資料庫）、`backups/`、`output/`（匯出的客戶名單）、
  `logs/`、`attachments/`、`custom_sources.yaml`
- 所有資料庫查詢都是參數化的，搜尋關鍵字的 `%` `_` 有跳脫
- 沒有 `eval` / `exec` / `pickle` / `shell=True`，沒有任何地方關閉 TLS 驗證
- 密碼不會進入日誌或錯誤訊息
- Gmail 讀信用 `BODY.PEEK`，不改變已讀狀態、不寄信、不刪信

### 你仍然要自己注意的

| 項目 | 說明 |
|---|---|
| 匯出的 Excel / CSV | **是明文**，就是完整客戶名單，寄給別人前想清楚 |
| 程式沒有登入機制 | 能開你電腦的人，就能用這個程式解開加密欄位 |
| 金鑰在你的 Windows 帳號裡 | **請先備份金鑰**，否則重灌後連備份都解不開（見下一節） |
| 公司名稱與統一編號 | 為了搜尋速度**維持明文**。這兩項不是個人資料 |
| 已經 commit 過的機密 | `.gitignore` 只擋未來。已經進版本庫的要用 `git rm --cached` 並考慮改密碼 |

### 資料庫加密

信箱、電話、地址、聯絡人姓名、備註、寄出的信件內容在寫進資料庫前會用
**AES-GCM** 加密；公司名稱、統一編號、產業別、網站維持明文，搜尋才不會變慢。
這條線是照《個人資料保護法》畫的——受規範的是自然人的資料，不是公司名稱。

```bash
python main.py encrypt      # 看目前狀態與加密了哪些欄位
```

**金鑰放在哪裡**：Windows 認證管理員（macOS 鑰匙圈／Linux Secret Service），
專案資料夾裡沒有金鑰，所以整個資料夾可以安全上 git，資料庫檔案被複製走也讀不出內容。

> ⚠️ **現在就去備份金鑰。** 金鑰跟著你的 Windows 使用者帳號走，**不會**跟著
> `crm.db` 一起被複製——連 `backups/` 裡的備份也是加密的。硬碟壞掉重灌之後，
> 備份檔還在，卻一個字都讀不出來。

**備份金鑰**：設定頁 →「資料庫加密」→「備份金鑰」，把那串字存到密碼管理員或印出來。
命令列是 `python main.py encrypt --export-key`。就一串 44 個字元，例如：

```
D65k4pfGeL_vZIP9gCw09GqlSW_hzXEBkw-btdhmwcQ=
```

**換電腦／重灌後**：把 `crm.db`（或一份 `backups/` 內的備份）複製回來，然後
設定頁 →「還原金鑰」貼回去就好。命令列是 `python main.py encrypt --import-key`
（會提示你貼上，不會留在指令歷史裡）。

沒有先備份金鑰就換電腦的話，程式會直接拒絕開啟並告訴你要匯入金鑰——這是刻意的，
否則畫面上的個資會全部變成空白，一存檔就把原本還救得回來的資料覆蓋掉。

**要關掉加密**：把 `config.yaml` 的 `database.encrypt` 改成 `false` 再啟動程式，
它會自動把資料轉回明文（轉換前一定會先自動備份一份）。改回 `true` 則自動加密回去。
兩個方向都是冪等的，中途關掉程式再開一次會從沒轉完的地方接下去。

**沒有系統憑證保管庫的環境**（部分 Linux、CI）會自動退回明文儲存並在啟動時警告，
不會寫出讀不回來的資料。

### 第一次上 git 的建議流程

```bash
python main.py security     # 必須通過
git init
git add .
git status                  # 再用眼睛確認一次清單
git commit -m "初始版本"
```

---

## 法律與使用規範

這個工具**只**處理網站公開顯示的資料，而且：

- 一定遵守 `robots.txt`（設定項 `crawler.respect_robots`，預設開啟）
- 每次請求之間有延遲與隨機抖動，並服從網站宣告的 `Crawl-delay`
- User-Agent 誠實表明身分並附上你的聯絡信箱
- **不會**繞過登入、付費牆或 CAPTCHA
- 信箱驗證只查 MX 紀錄，**不會**用 SMTP 探測個別信箱

`robots.txt` 抓不到或回 5xx 時，程式會保守判定為「不允許」而不是猜測。
只有在你自己擁有的網站上，才適合把 `respect_robots` 關掉。

**寄送開發信受《個人資料保護法》規範：**
- 只能寄給你有正當來源取得的商業聯絡資訊，且用途須與業務相關
- 必須提供明確、可執行的退訂方式（程式已強制附加）
- 收到退訂要求後應立即停止聯絡，且不得再次寄送
- 請保留寄送紀錄（`email_messages` 資料表）作為稽核依據

大量或高頻率寄送也可能違反 Gmail 服務條款導致帳號被鎖，
`daily_limit` 與 `delay_seconds` 的預設值刻意保守，不建議調高。

---

## 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| 點 `start.bat` 沒反應 | 先執行 `console.bat` 再輸入 `python main.py gui`，就看得到錯誤訊息。也可查 `logs/error.log` |
| 中文顯示成方框 | 系統缺少中文字型。程式會依序尋找 Microsoft JhengHei UI / Noto Sans TC 等 |
| 爬取結果 0 筆 | 該頁可能是 JavaScript 產生的。把 `crawler.engine` 改成 `playwright` 並執行過 `playwright install chromium` |
| 「robots.txt disallows」 | 對方網站明確禁止爬取該路徑。請改用其他來源 |
| 信箱都顯示「查無 MX」 | 該網域不存在或不收信；若全部都這樣，檢查網路與 DNS |
| Gmail 登入失敗 | 確認用的是**應用程式密碼**不是帳號密碼，且已開啟兩步驟驗證 |
| 寄送按鈕是灰的 | `mailer.enabled` 還是 `false`，或帳號尚未在「設定」頁設定完成 |
| 排程沒有執行 | 排程只在程式開著時運作。要無人值守請用 `python main.py schedule` |
| 改了 `config.yaml` 沒生效 | 需要重新啟動程式 |
| `DLL load failed ... 應用程式控制原則已封鎖此檔案` | Windows 11 的**智慧型應用程式控制**擋掉了剛下載、還沒有信譽的套件二進位檔（實際遇過 pandas 3.0.5）。改裝有信譽的舊版本即可，例如 `python -m pip install "pandas>=2.2,<3.0"` |
| VS Code 顯示套件「未安裝」但指令列正常 | 編輯器還指著舊的直譯器。`Ctrl+Shift+P` → `Python: Select Interpreter` → 選目前資料夾的 `.venv\Scripts\python.exe` |

---

## 授權

本專案以 **MIT License** 發佈，可自由使用、修改、重新發佈（含商業用途），
但**須保留原著作權聲明**。完整條文見 [`LICENSE`](LICENSE)。

MIT 授權的是**這個工具的程式碼**，不包含你用它蒐集到的資料。你怎麼使用蒐集
到的聯絡資訊，仍受《個人資料保護法》、目標網站的使用條款、以及上方
[使用條款與免責聲明](#-使用條款與免責聲明)的規範。

---

## 為什麼沒有現成的 exe 可以下載

專案裡有 `roster.spec`，跑 `python -m PyInstaller roster.spec` 就會產出
`dist/Roster/Roster.exe`。但**這裡不提供打包好的執行檔下載**，原因是：

Windows 11 的 **Smart App Control** 會封鎖沒有數位簽章的執行檔，而且**沒有
「仍要執行」的例外選項**（跟舊的 SmartScreen 不同，SmartScreen 至少讓你按
「其他資訊 → 仍要執行」）。要讓它放行，需要程式碼簽章憑證：OV 憑證一年
約台幣 6,000–15,000，而且簽了之後還要累積信譽才會被信任；要立即被信任得買
更貴的 EV 憑證。

對一個沒有商業收入的個人專案，這個成本不成比例。所以：

- **從原始碼執行**（`start.bat`）完全不受影響——它跑的是有微軟簽章的
  `python.exe`
- 自己打包出來的 exe，在**沒有開啟 Smart App Control 的機器上**（Windows 10、
  多數升級上來的 Windows 11、公司管控的機器）可以正常執行
- 打包設定留在專案裡，之後若取得憑證，在 `roster.spec` 後面加一步簽章即可

---

## 給開發者

```
Roster/
├── main.py               命令列進入點（Typer）
├── config.yaml           所有設定（core/config.py 以 pydantic 驗證）
├── custom_sources.yaml   精靈儲存的自訂來源（自動產生）
├── assets/               應用程式圖示（多尺寸 .ico 與去背原圖）
├── core/                 設定、憑證、日誌、排程、共用列舉與 DTO
├── controllers/          控制器層：介面與資料之間唯一的接縫
├── crawler/              robots 政策、抓取引擎、解析、自動偵測、爬取流程
│   └── sources/          來源註冊表：sample（離線）、generic_html（設定驅動）
├── database/             ORM 模型、Session、Repository、備份、結構遷移
├── verifier/             正規化、驗證、MX 查詢、重複偵測
├── exporter/             Excel / CSV / JSON 匯出、試算表匯入、匯入範例檔
├── gmail/                IMAP 讀信、SMTP 寄信、樣板、寄送流程
├── gui_qt/               桌面介面（PySide6）
├── templates/            離線範例名錄、郵件樣板
├── tests/                pytest 測試
└── logs/ output/ data/ backups/
```

分層規則：**View 不碰資料庫，其他層不碰 UI。**
所有讀寫都經過 Repository，交易邊界由 `database.session.session_scope` 掌管。
介面只呼叫 `controllers/`，拿回純資料物件——這一層就是當初把介面從
customtkinter 換成 PySide6 時，後端一行都不用動的原因。

### 兩個踩過的坑，動到相關程式碼前先看一下

**背景執行緒裡不要第一次 import 模組。** PySide6 會裝一個 import 掛勾，
從 `QThreadPool` 借出來的執行緒觸發它時，整個行程會 `Fatal Python error:
Aborted`——沒有例外可以接。控制器用的是延遲 import，所以啟動時要先用
`core.preload.preload()` 在主執行緒把它們載入一次。新增背景工作會用到的
模組時，記得加進 `core/preload.py` 的清單。

**背景執行緒裡不要做例外內省。** `traceback.format_exc()` 之類同樣會不定時
炸掉。`gui_qt/tasks.py` 的 worker 只把例外物件本身 emit 回 UI 執行緒，
格式化在那邊做。

```bash
python -m pytest                                            # 全部測試
python -m pytest --cov=core --cov=crawler --cov=database \
                 --cov=exporter --cov=verifier --cov=gmail  # 覆蓋率
```

測試不連網、不查 DNS、不需要 Gmail 帳號、不寫入系統憑證保管庫，全部以 stub 取代。

**時間格式約定**：資料庫中所有時間戳記都是**無時區的本機時間**。
這是單人桌面工具，存本機時間讓「今天新增」這類查詢與匯出欄位直接符合直覺。

**新增爬取來源類型**：繼承 `crawler.base.BaseSource` 實作 `iter_pages()`，
再用 `crawler.sources.register_source()` 註冊，爬取流程本身不必更動。

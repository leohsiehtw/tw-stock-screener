# tw-stock-screener 台股盤後選股

收盤後掃描全市場（上市＋上櫃＋ETF），依「先過濾、再確認、最後挑進場點」找出突破型／回測型候選與建議停損。
資料直接取自證交所與櫃買中心官方網站，程式只用 Python 內建模組，不需安裝任何套件。

> **免責聲明**：本專案僅供程式與技術分析研究，不是投資建議，也不保證任何績效。使用者應自行判斷並承擔所有投資風險。資料可能延遲、缺漏或有誤，請以交易所公告為準。

## 需求

- Python 3.8 以上
- Windows（`run_daily.bat` 為批次檔；`fetch_data.py`、`screener.py` 本身跨平台）
- 可連線到 www.twse.com.tw 與 www.tpex.org.tw

## 檔案

| 檔案 | 用途 |
|---|---|
| screener.py | 選股主程式 |
| fetch_data.py | 抓全市場日 K、除權息還原，建立 `data/` |
| run_daily.bat | 每日一鍵：更新資料、選股、存到 `結果/` |
| 自選股.範例.csv | 自選股清單範例（代號,名稱,備註）。複製成 `自選股.csv` 後改成自己的 |
| data/ | 每檔一個 `<代號>.csv`（程式產生）：`date,open,high,low,close,volume,raw_close,name` |
| cache/ | 原始每日快照與除權息事件（程式產生，可續跑） |
| 結果/、logs/ | 選股結果與執行紀錄（程式產生） |

## 關卡

| 關 | 條件 |
|---|---|
| 0 週線 | 週 MACD 的 DIF > 0（零軸上）或週柱剛翻紅；收盤在週 MA13、週 MA26 之上；週 MA5 < MA13 < MA26（空頭排列）直接剔除 |
| 1 趨勢 | 收盤 > MA60、MA20 > MA60、MA20 不下彎且 MA60 上彎。差距沒擴大會加註「整理中」，不剔除 |
| 2 動能 | 日 OSC 今天由負翻正；或 3 日內 DIF 上穿 DEA 且今日柱 > 昨日柱 |
| 3 量能 | 今日量 > 前 5 日均量 × 1.3，且收紅、收盤高於昨收 |
| A 突破型 | 過 0～3 關，且前 20 日箱型（高低差 ≤ 12%）或布林縮口（帶寬在近 120 日最低 25%）後，帶量長紅 K（實體 ≥ 3%）收在布林上軌之上；今天突破，或昨天突破、今天回測守住 |
| B 回測型 | 過 0～1 關（不看 2、3 關），近 20 日至少 80% 收在 MA20 上、MA20 上彎，今天低點回到 MA20 ±2% 內、收在 MA20 上止跌，量 < 5 日均量 × 0.8 |

- 停損：突破型取「長紅 K 低點」與「MA20」中離收盤較近者；回測型取 MA20 與當日低點較低者再減 0.5%。
- 部位：加 `--capital 總資金`，會試算單筆虧損 2% 與 5% 時的股數。
- 自選股：`scan --list 自選股.csv` 另外列出「過第 2、3 關但型態還沒到」的股票。
- 所有門檻集中在 `screener.py` 開頭的 `P`，要調整改那裡。
- 指標使用還原權息價；結果中的價格顯示未還原的實際收盤（`raw_close`）。

## 安裝與用法

```
git clone https://github.com/<你的帳號>/tw-stock-screener.git
cd tw-stock-screener
copy 自選股.範例.csv 自選股.csv

python fetch_data.py backfill      # 第一次：補約 430 天歷史，約 45～60 分鐘，可中斷續跑
python fetch_data.py daily         # 之後每天收盤後增量更新並重建 data/
python fetch_data.py build         # 只用 cache 重建 data/（離線）

python screener.py scan --data data --list 自選股.csv --capital 1000000 --out 盤後選股.md --txt 盤後選股.txt --json 盤後選股.json
python screener.py pool --data data --out 自選股候選.csv

run_daily.bat          # 一鍵：週末略過，交易日更新資料並選股，輸出到 結果\盤後選股_YYYYMMDD.*
run_daily.bat force    # 略過週末檢查
```

要每天自動執行，可用 Windows 工作排程器呼叫 `run_daily.bat`（本專案不會自動建立排程）。
`fetch_data.py daily` 若在 13:30～16:00 之間跑到資料尚未公布，會每 10 分鐘重試；16:00 後仍沒資料視為休市，不產生報告。

## 資料來源與限制

來源（官方，不經第三方）：
- 上市日收盤：證交所 `rwd/zh/afterTrading/MI_INDEX`
- 上櫃日收盤：櫃買中心 `www/zh-tw/afterTrading/otc`
- 除權息：證交所 `exRight/TWT49U`、櫃買中心 `bulletin/exDailyQ`

處理方式：
- 只收 4 碼一般股與 ETF（含槓桿、反向、債券型），剔除權證、特別股、-DR；最新交易日沒成交的不輸出。
- 請求間隔 3.5 秒，失敗等 10、30、90 秒重試；請勿調小間隔以免被封鎖。
- 可續跑：每個交易日一個快取檔，已有的不重抓；寫檔先寫 `.tmp` 再替換。
- 休市判斷：官方回「無資料」且日期已過才標記休市；當天沒資料不標記。
- 除權息還原：係數 = 參考價 ÷ 除權息前收盤價，對該日之前的 K 棒由新往舊連乘，調整 open/high/low/close，成交量不動；`data/` 每次由 `cache/` 整批重建，不會重複還原。

限制：
- 減資、分割、反分割不調整，遇到時該檔前後價格會斷層。
- 官方網站格式或網址可能改版，改版後需更新 `fetch_data.py`。
- 不要把 `screener.py add` 與 `fetch_data.py` 的資料混用。
- 結果僅反映技術面條件，沒有基本面、消息面與流動性的判斷。

## 授權

MIT License，詳見 [LICENSE](LICENSE)。

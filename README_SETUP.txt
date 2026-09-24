2560 Cloud Monitor — Setup

這包共有：
1. 2560_cloud_monitor.py
2. .github/workflows/2560-monitor.yml

運作方式：
- GitHub Actions 每天 UTC 00:10 / 04:10 / 08:10 / 12:10 / 16:10 / 20:10 執行。
- 只使用已完成的 4H 與 1D K 線。
- 若最新完成的 4H 本身是新的 Strict 訊號，就透過 ntfy 推送到手機。
- 不需要 Gate API Key，不會下單。

GitHub Secrets：
NTFY_TOPIC = 你自己設定的一串很難猜的 topic 名稱
NTFY_SERVER = https://ntfy.sh

手機：
1. 安裝 ntfy App
2. Subscribe 到同一個 topic
3. GitHub Actions 有 Strict 時就會收到推播

重要：
不要把 NTFY_TOPIC 寫進公開程式碼；放 GitHub Repository Secret。

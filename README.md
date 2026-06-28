# A股晚间复盘云端定时推送

这套脚本用于每天北京时间 21:00 在 GitHub Actions 云端运行：

- 查询 A股主要指数和高成交个股公开行情
- 生成当天复盘与隔天 1 只观察候选
- 推送到 WxPusher App

## GitHub Secrets

在 GitHub 仓库里进入 `Settings -> Secrets and variables -> Actions`，添加：

- `OPENAI_API_KEY`：OpenAI API Key，用于生成复盘文字
- `WXPUSHER_APP_TOKEN`：WxPusher 应用 appToken
- `WXPUSHER_UIDS`：你的 WxPusher UID，多个用英文逗号分隔

可选变量：

- `OPENAI_MODEL`：默认 `gpt-5`

## 运行时间

`.github/workflows/daily-a-share-review.yml` 使用 UTC `13:00`，对应北京时间 `21:00`。

## 手动测试

在 GitHub Actions 页面选择 `Daily A-share review`，点击 `Run workflow`。

## 免责声明

脚本输出仅供研究观察，不构成投资建议，不承诺收益。


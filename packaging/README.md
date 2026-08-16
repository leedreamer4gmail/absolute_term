# absolute_term 客户端打包

## 工具（同 livestream / 截图）

| 工具 | 作用 | 位置 |
|------|------|------|
| PyInstaller | Python → `极限词扫描.exe` onedir | 项目 `.venv\`；`packaging\build_onedir.ps1` |
| Inno Setup 7 (`ISCC.exe`) | 打单文件 `Setup.exe`（payload 打进安装包） | `E:\Program Files\Inno Setup 7\ISCC.exe` |
| PowerShell | 入口脚本 | `packaging\` |

**不要**交付源码 zip / portable zip。只交付 `Setup.exe`。

## 流水线

```powershell
cd D:\project\absolute_term
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_onedir.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_installer.ps1 -Publish
```

产出：`packaging\out\absolute_term\Setup.exe`  
上传：`www/downloads/absolute_term_Setup.exe` + `absolute_term_update_Setup.exe`

版本三处同号同抬：`client/version.py` = `absolute.iss MyAppVersion` = 云端 `[client_release] client_app_version`

## 反逆向 / SmartScreen

- 不把 `.py` 放进 datas；打包前删掉 dist 里残留 `.py`
- 交付物是编译后的 onedir + Inno 安装包，不是仓库源码
- **SmartScreen「Windows 已保护你的电脑」**：因为安装包未做微软 Authenticode 代码签名（或新发布者尚无信誉）。**重打未签名包解决不了。**
  - 用户临时：点「更多信息」→「仍要运行」
  - 根治：购买 OV/EV 代码签名证书，在 `config.ini.local` 配 `[codesign]`，打包时自动跑 `packaging\sign.ps1`

; absolute_term 完整安装包 Inno（参考 livestream：Setup.exe + 同级 payload）
; 用户：解压目录 → 运行 Setup.exe
#define MyAppName "小李的电商扫描器"
#define MyAppVersion "1.6.1"
#define MyAppPublisher "leedreamer"
#define MyAppExeName "极限词扫描.exe"

[Setup]
AppId={{A3B7C1D2-E4F5-6789-ABCD-EF0123456789}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=out
OutputBaseFilename=Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
; 打进 Setup.exe（单文件安装；不依赖旁路 payload，也不带 .py 源码）
Source: "payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; 源文件/Excel 写在 {app}\file\<用户名>\ ，必须给普通用户写权限，否则装进 Program Files 新用户扫不了
Name: "{app}\file"; Permissions: users-modify

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Ini]
; 写进 _internal，勿在 {app} 根目录另造一份只有版本号的空 config.ini
Filename: "{app}\_internal\config.ini"; Section: "client_release"; Key: "client_app_version"; String: "{#MyAppVersion}"

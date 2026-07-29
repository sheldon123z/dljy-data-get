# 依赖与自包含说明

## 最终用户

Windows 便携版已经包含 Python 解释器、标准库和全部第三方包。最终用户不需要安装
Python、Node.js、npm、Docker 或 Excel，只需完整解压 ZIP 并双击
`电力现货价格工作台.exe`。

浏览器使用 Windows 已安装的默认浏览器。采集数据需要能访问电查查接口；大模型总结
需要能访问用户选择的 DeepSeek、智谱 GLM 或 OpenAI 兼容接口。

## Python 运行时依赖

| 依赖 | 锁定版本 | 用途 |
| --- | --- | --- |
| Python | 3.11—3.13，构建机使用 3.13 | 程序运行时 |
| openpyxl | 3.1.5 | Excel 导入与导出 |
| et-xmlfile | 2.0.0 | openpyxl 的 XML 写入依赖 |

HTTP 请求、CSV、JSON、本地网站、线程、加密随机数和模型接口均使用 Python 标准库，
因此没有额外运行时框架。

## Windows 构建依赖

| 依赖 | 锁定版本 | 用途 |
| --- | --- | --- |
| PyInstaller | 6.15.0 | 打包 Python 解释器和项目代码 |
| altgraph | 0.17.5 | PyInstaller 模块依赖图 |
| pefile | 2023.2.7 | Windows PE 文件处理 |
| pyinstaller-hooks-contrib | 2026.6 | 第三方模块打包钩子 |
| pywin32-ctypes | 0.2.3 | Windows 打包接口 |
| packaging | 26.2 | 依赖版本解析 |
| setuptools | 83.0.0 | 构建工具链 |
| PowerShell | Windows 10/11 自带 | 执行自动构建脚本 |

构建依赖列在 `requirements-build.txt`。网站托管版的 JavaScript 依赖由
`web/package-lock.json` 完整锁定。

## 鉴权与密钥

- 数据采集 Authorization：在首页“① 填写数据 Authorization”处填写。
- 模型 API Key：在首页“② 模型 API Key”处填写，Agent 协作方式在“③ Agent 协作模式”选择。
- 本地版仅写入应用目录的 `.env`，不会进入任何导出文件。
- 托管版使用服务端加密存储，浏览器只接收脱敏状态。

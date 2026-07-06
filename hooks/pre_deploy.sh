#!/bin/bash
# pre-hook — 建立 deploy 目標與 LaunchAgent 依賴的目錄（舊 deploy.sh 的 mkdir -p）。
# 前置 setup，在 engine 寫任何檔之前跑：
#   - ~/Library/LaunchAgents：channel-watchdog.plist 的落點（wsdeploy 原子寫需要父目錄存在）
#   - $OPENCLAW_WORKSPACE/logs：watchdog plist 的 stdout/stderr 落點（非部署 target，需先存在）
# mkdir -p 冪等；env OPENCLAW_WORKSPACE / HOME 由 engine 注入。
set -euo pipefail
mkdir -p "$HOME/Library/LaunchAgents" "$OPENCLAW_WORKSPACE/logs"

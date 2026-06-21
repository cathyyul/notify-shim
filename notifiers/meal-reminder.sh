#!/bin/zsh
# meal-reminder — prompt Yuting to log a meal. Fans out via the notify-dm shim
# (Telegram + LINE). Triggered by LaunchAgents at breakfast/lunch/dinner.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 breakfast|lunch|dinner" >&2
  exit 1
fi

meal="$1"
case "$meal" in
  breakfast) text='🍽️ 早餐時間到！吃了什麼？拍照或文字告訴我，我幫你記。' ;;
  lunch)     text='🍽️ 午餐時間到！吃了什麼？拍照或文字告訴我，我幫你記。' ;;
  dinner)    text='🍽️ 晚餐時間到！吃了什麼？拍照或文字告訴我，我幫你記。' ;;
  *) echo "unknown meal: $meal" >&2; exit 2 ;;
esac

# notify-dm is deployed alongside this script in workspace scripts/.
NOTIFY_DM="${NOTIFY_DM_BIN:-${0:A:h}/notify-dm}"
exec "$NOTIFY_DM" "$text"

#!/usr/bin/env bash
#
# auto_release.sh — автоматизация релизов с обновлением CHANGELOG, коммитом и пушем.
#
# Этот скрипт предназначен для использования в корне репозитория. Он
# автоматически определяет последний тег версии, генерирует следующую
# версию (увеличивая патч-номер), собирает commit‑сообщения с момента
# последнего тега, добавляет раздел в CHANGELOG.md, коммитит все
# изменения, создаёт аннотированный тег и отправляет всё в удалённый
# репозиторий. Перед запуском убедитесь, что у вас настроен git remote
# "origin" и есть права на push.

set -euo pipefail

# Определяем последний тег (в формате vX.Y.Z). Если тега нет — считаем 0.0.0.
last_tag=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
echo "Последний тег: $last_tag"

# Убираем префикс 'v' и разбиваем версию на части
version=${last_tag#v}
IFS='.' read -r major minor patch <<<"$version"
# Если не удалось разобрать, сбросим в нули
major=${major:-0}
minor=${minor:-0}
patch=${patch:-0}

# Увеличиваем патч-номер
new_patch=$((patch + 1))
new_version="$major.$minor.$new_patch"
today=$(date +%Y-%m-%d)
echo "Новая версия: $new_version (дата: $today)"

# Собираем список commit-сообщений с момента последнего тега
commit_msgs=$(git log --pretty=%s "${last_tag}"..HEAD)
if [[ -z "$commit_msgs" ]]; then
  echo "Нет новых коммитов после последнего тега. Завершение."
  exit 0
fi

# Формируем новый раздел changelog
changelog_header="## [$new_version] - $today"
changelog_entries=""
while IFS= read -r line; do
  [[ -n "$line" ]] && changelog_entries+="- $line\n"
done <<< "$commit_msgs"
changelog_section="$changelog_header\n$changelog_entries\n"

# Вставляем раздел в начало CHANGELOG.md (создаём файл, если его нет)
changelog_file="CHANGELOG.md"
if [[ -f "$changelog_file" ]]; then
  cp "$changelog_file" "${changelog_file}.bak"
  printf "%b" "$changelog_section" >"$changelog_file"
  cat "${changelog_file}.bak" >>"$changelog_file"
  rm "${changelog_file}.bak"
else
  printf "%b" "# Changelog\n\n" >"$changelog_file"
  printf "%b" "$changelog_section" >>"$changelog_file"
fi
echo "CHANGELOG.md обновлён."

# Добавляем изменения в индекс и коммитим
git add "$changelog_file" .
git commit -m "chore(release): v$new_version\n\n$changelog_entries"
echo "Коммит создан."

# Создаём аннотированный тег
git tag -a "v$new_version" -m "Release v$new_version"
echo "Тег v$new_version создан."

# Отправляем в удалённый репозиторий
git push origin HEAD
git push origin "v$new_version"
echo "Изменения и тег отправлены в origin. Релиз завершён."

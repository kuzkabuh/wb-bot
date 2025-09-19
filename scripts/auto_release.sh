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

# целевая ветка для пуша (основная ветка репозитория)
target_branch="main"

# Убедимся, что мы на нужной ветке. Если нет, переключаемся.
current_branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$current_branch" != "$target_branch" ]]; then
  echo "Вы на ветке $current_branch. Переключаюсь на $target_branch для релиза."
  git checkout "$target_branch"
fi

# -----------------------------------------------------------------------------
# Сборка архива проекта для отправки/резервного копирования
# Используем git archive для создания zip‑архива текущего состояния HEAD.
release_name="release_$(date +%Y%m%d%H%M%S).zip"
git archive --format=zip --output="$release_name" HEAD
echo "Создан архив $release_name для отправки."

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
# Если после последнего тега нет новых коммитов, но в рабочем дереве
# имеются несохранённые изменения, мы будем использовать список изменённых
# файлов в качестве описания изменений.  Это позволяет выпускать версии
# даже без промежуточных коммитов.
if [[ -z "$commit_msgs" ]]; then
  # Получаем список изменённых файлов (staged и unstaged)
  changed_files=$(git status --porcelain | awk '{print $2}')
  if [[ -z "$changed_files" ]]; then
    echo "Нет новых коммитов и нет изменений в рабочем каталоге. Завершение."
    exit 0
  fi
  commit_msgs=""
  for f in $changed_files; do
    commit_msgs+="Modified file: $f\n"
  done
fi

# Разбираем commit‑сообщения и раскладываем их по категориям в стиле Keep a Changelog.
added_entries=""
changed_entries=""
fixed_entries=""
while IFS= read -r msg; do
  # Определяем тип по префиксу commit‑сообщения (conventional commits)
  lower=$(echo "$msg" | tr '[:upper:]' '[:lower:]')
  if [[ $lower =~ ^feat ]]; then
    added_entries+="- ${msg}\n"
  elif [[ $lower =~ ^fix ]]; then
    fixed_entries+="- ${msg}\n"
  else
    changed_entries+="- ${msg}\n"
  fi
done <<< "$commit_msgs"

# Формируем новый раздел changelog
changelog_section="## [$new_version] - $today\n"
if [[ -n "$added_entries" ]]; then
  changelog_section+="### Added\n$added_entries\n"
fi
if [[ -n "$changed_entries" ]]; then
  changelog_section+="### Changed\n$changed_entries\n"
fi
if [[ -n "$fixed_entries" ]]; then
  changelog_section+="### Fixed\n$fixed_entries\n"
fi
changelog_section+="\n"

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

# Добавляем изменения в индекс и коммитим. Текст коммита включает все пункты из changelog
git add "$changelog_file" .
commit_body=""
commit_body+="$added_entries$changed_entries$fixed_entries"
git commit -m "chore(release): v$new_version\n\n$commit_body"
echo "Коммит создан."

# Создаём аннотированный тег
git tag -a "v$new_version" -m "Release v$new_version"
echo "Тег v$new_version создан."

# Отправляем коммит и тег в удалённый репозиторий. Используем основную ветку.
git push origin "$target_branch"
git push origin "v$new_version"
echo "Изменения отправлены в origin/$target_branch и тег v$new_version создан. Релиз завершён."

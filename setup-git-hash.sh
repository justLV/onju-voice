#!/bin/sh

if [ ! -f .git/hooks/post-commit ]; then
  cp setup-git-hash.sh .git/hooks/post-commit
  chmod +x .git/hooks/post-commit
  echo "Setup, will automatically update git hash post commit"
fi

git_hash=$(git rev-parse --short HEAD)

echo "#define GIT_HASH \"${git_hash}\"" > onjuino/git_hash.h
echo "🚀 Updated git hash to ${git_hash}"

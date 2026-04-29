# GitHub Actions + Docker GHCR + TrueNAS Setup Guide

Setup prywatnego repozytorium GitHub z publicznym Docker image na GHCR, gotowym do TrueNAS SCALE.

---

## 1. Warunki wstępne

```bash
# GitHub CLI
brew install gh  # macOS/Linux
# Windows: https://github.com/cli/cli/releases

# Zaloguj się w GitHub CLI
gh auth login
```

---

## 2. Utwórz Personal Access Token (PAT)

```bash
# Generowanie PAT (Classic) z uprawnieniami do packages
gh auth refresh --scopes write:packages,read:packages
```

**Lub ręcznie:**
1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token
3. Zaznacz: `write:packages`, `read:packages`
4. Skopiuj token → stwórz secret w repo

---

## 3. Dodaj token do Secret w repozytorium

```bash
# Automatycznie
gh secret set DOCKER_PUSH_TOKEN --body "YOUR_TOKEN_HERE"

# Lub ręcznie
# Repo → Settings → Secrets and variables → Actions → New repository secret
# Name: DOCKER_PUSH_TOKEN
# Value: <skopiuj_token>
```

---

## 4. Utwórz GitHub Actions workflow

Plik: `.github/workflows/docker-image.yml`

```yaml
name: Build Docker image on commit

on:
  push:
    branches:
      - main

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to GitHub Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.DOCKER_PUSH_TOKEN }}

      - name: Build and push Docker image
        uses: docker/build-push-action@v5
        with:
          context: ./docker-backend
          file: ./docker-backend/Dockerfile
          push: true
          tags: ghcr.io/${{ github.repository }}:latest
```

---

## 5. Commit i push workflow

```bash
git add .github/workflows/docker-image.yml
git commit -m "Add: GitHub Actions Docker build workflow"
git push origin main
```

GitHub Actions zbuduje obraz i wypchnie do GHCR.

---

## 6. **WAŻNE: Zmień widoczność pakietu na PUBLIC**

```bash
# Po pierwszym buildie, zmień pakiet na public
gh api --method PATCH /user/packages/container/YOUR_REPO_NAME/permissions \
  -f visibility=public
```

**Lub ręcznie:**
https://github.com/users/YOUR_USERNAME/packages/container/YOUR_REPO_NAME/settings

Zmień **Visibility** → **Public**

---

## 7. Przygotuj docker-compose.yml do TrueNAS

```yaml
services:
  app-name:
    image: ghcr.io/YOUR_USERNAME/YOUR_REPO:latest
    ports:
      - "8000:8000"
    environment:
      - VAR1=value1
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

**Ważne:**
- Usuń `version: "3.9"` (TrueNAS je ignoruje)
- Użyj publicznego Docker image z GHCR

---

## 8. Wdróż na TrueNAS

```bash
# SSH do TrueNAS hosta
ssh root@truenas-ip

# Zaloguj się do GHCR (opcjonalnie, jeśli image public)
docker login ghcr.io -u YOUR_USERNAME -p YOUR_TOKEN

# Uruchom Docker Compose
cd /mnt/Applications/DockerApps/app-name
docker compose up -d
```

---

## 9. Weryfikacja

```bash
# Sprawdź czy obraz jest dostępny publicznie
curl -I https://ghcr.io/v2/YOUR_USERNAME/YOUR_REPO/manifests/latest

# Powinno zwrócić 200, nie 401 Unauthorized
```

---

## 10. TrueNAS APPS - Dodaj Custom App

1. TrueNAS SCALE → Applications → Discover
2. Custom App → Create
3. Wklej docker-compose.yml
4. Deploy

Obraz powinien się ściągnąć i uruchomić bez błędów.

---

## Troubleshooting

### "Error: unauthorized" przy `docker pull`

```bash
# Obraz jest prywatny - zmień visibility na public
# https://github.com/users/YOUR_USERNAME/packages/container/YOUR_REPO/settings
```

### "version is obsolete" warning

```bash
# Usuń linię "version" z docker-compose.yml
```

### "Additional property auth is not allowed"

```bash
# Usuń sekcję "auth" z docker-compose
# Docker Compose w TrueNAS nie obsługuje "auth"
```

---

## Command Cheat Sheet

```bash
# List personal tokens
gh auth status

# Check Docker image visibility
gh api /user/packages/container/YOUR_REPO/permissions

# View workflow runs
gh run list -R YOUR_USERNAME/YOUR_REPO

# View workflow logs
gh run view RUN_ID -R YOUR_USERNAME/YOUR_REPO --log
```

---

## Podsumowanie

✅ Repo: **prywatne**
✅ Docker image: **publiczny** (GHCR)
✅ TrueNAS APPS: **działa bez logowania**
✅ Automatyczne buil na każdy push do `main`


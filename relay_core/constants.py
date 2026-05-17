from __future__ import annotations
from pathlib import Path

VERSION = "0.5.0"
MAX_HISTORY_DISPLAY = 20
MAX_HANDOFF_WORDS = 250
MAX_DIFF_PROMPT_CHARS = 6000
MAX_HANDOFF_PROMPT_CHARS = 1800
MAX_DECISIONS_LOG = 50
GLOBAL_CONFIG_DIR = Path.home() / ".relay"

INSTALL_HINTS: dict[str, str] = {
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "codex": "npm i -g @openai/codex\ncodex",
    "git": "https://git-scm.com/install",
}

SENSITIVE_PATH_PATTERNS = (
    ".env",
    ".pem",
    ".key",
    "secrets",
    "credentials",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "migrations",
    "auth",
    "stripe",
    "payment",
)

FRONTEND_KEYWORDS = [
    "frontend", "ui", "ux", "react", "next.js", "nextjs",
    "component", "components", "tailwind", "css", "html",
    "landing", "landing page", "copy",
    "animation", "responsive", "layout", "design",
    "hero", "navbar", "pricing", "form styling",
    "style", "styles", "styled", "theme", "icon",
    "modal", "drawer", "sidebar", "card", "button",
    "typography", "font", "color", "palette", "dark mode",
]

BACKEND_KEYWORDS = [
    "backend", "api", "route", "server", "database",
    "sql", "postgres", "supabase", "auth", "migration",
    "schema", "test", "tests", "bug", "error",
    "performance", "security", "script", "worker",
    "cron", "queue", "endpoint", "validation",
    "middleware", "webhook", "cache", "redis",
    "model", "repository", "service", "handler",
    "environment", "env", "config", "deploy",
]

FRONTEND_EXTENSIONS = [".tsx", ".jsx", ".css", ".scss", ".html", ".svelte", ".vue"]
BACKEND_EXTENSIONS = [".py", ".go", ".rs", ".sql", ".java", ".rb", ".php", ".ts", ".js"]

FRONTEND_PATHS = ["components/", "app/page.tsx", "styles/", "public/", "pages/", "src/ui/", "src/components/"]
BACKEND_PATHS = ["app/api/", "api/", "server/", "lib/db/", "db/", "migrations/", "tests/", "schema.prisma", "src/server/", "src/api/"]

RATE_LIMIT_PATTERNS = (
    "rate limit", "usage limit", "quota", "quota exceeded",
    "too many requests", "try again later", "exceeded limit",
)

CLEAR_UI_WORDS = {"ui", "ux", "design", "responsive", "layout", "hero", "navbar", "pricing", "css", "style", "theme"}

COMPLEX_TASK_SIGNALS = [" and then ", " and ", " then ", " also ", " plus ", " as well as ", " including ", ", "]
COMPLEX_TASK_VERBS = ["add", "implement", "build", "create", "integrate", "set up", "configure", "wire up", "connect", "fix"]

FRAMEWORK_MARKERS: dict[str, str] = {
    "next.config.js": "nextjs",
    "next.config.ts": "nextjs",
    "next.config.mjs": "nextjs",
    "vite.config.ts": "vite",
    "vite.config.js": "vite",
    "angular.json": "angular",
    "vue.config.js": "vue",
    "nuxt.config.ts": "nuxt",
    "svelte.config.js": "svelte",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java",
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "setup.py": "python",
    "Gemfile": "ruby",
    "package.json": "node",
}

ALL_COMMANDS = [
    "review", "ai-review", "summary", "commit", "push", "doctor", "status",
    "history", "why", "continue", "chain", "audit",
    "config", "scan", "interactive", "init", "context", "digest",
    "auto", "plan", "brain", "watch", "every", "on", "triggers", "trigger-check",
]

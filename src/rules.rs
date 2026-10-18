//! Built-in process heuristics. User additions come from `prefs`.

/// Processes a dev server's ownership walk may climb through.
pub const RUNNERS: &[&str] = &[
    "node", "MainThread", "next-server", "vite", "esbuild", "tsx", "ts-node", "workerd", "nest",
    "npm", "npx", "pnpm", "yarn", "bun", "bunx", "deno", "turbo", "nx",
    "python", "python3", "Python", "uv", "poetry", "pipenv", "uvicorn", "gunicorn",
    "ruby", "bundle", "rails", "go", "air", "cargo", "java", "gradle", "mvn",
    "dotnet", "php", "mix", "beam.smp", "elixir",
];

/// Processes allowed to be reported as a listening dev server.
pub const DEFAULT_ALLOWLIST: &[&str] = &[
    "node", "MainThread", "next-server", "vite", "esbuild", "tsx", "ts-node", "workerd", "nest",
    "npm", "npx", "pnpm", "yarn", "bun", "bunx", "deno", "turbo", "nx",
    "python", "python3", "Python", "uv", "poetry", "pipenv", "uvicorn", "gunicorn", "flask",
    "django", "celery", "ruby", "bundle", "rails", "puma", "unicorn", "php", "php-fpm",
    "java", "gradle", "mvn", "go", "air", "cargo", "rustc", "dotnet", "beam.smp", "elixir",
    "mix", "nginx", "httpd", "apache", "apache2", "caddy", "postgres", "mysql", "mysqld",
    "mariadbd", "redis-server", "mongod", "docker-proxy", "rootlesskit", "pasta", "slirp4netns",
];

pub const DEFAULT_PROTECTED: &[&str] = &[
    "postgres", "redis-server", "mongod", "mysqld", "mariadbd", "mysql", "docker-proxy",
];

pub const EXCLUDED_DESKTOP_BINARIES: &[&str] = &[
    "electron", "cursor", "code", "claude-desktop", "antigravity", "language_server", "chrome",
    "chromium", "firefox", "brave", "grok-bot",
];

/// Sockets held by any of these are IDE internals, not dev servers.
pub const IDE_SOCKET_OWNERS: &[&str] = &["antigravity", "language_server", "cursor", "code", "claude-desktop"];

pub const SHELLS: &[&str] = &["sh", "bash", "zsh", "dash", "fish"];
pub const AGENT_NAMES: &[&str] = &["claude", "codex", "Conductor"];
pub const AGENT_PATH_MARKERS: &[&str] = &["@anthropic-ai/claude-code", "com.conductor.app", "/codex/"];

/// Node-hosted CLIs that should be shown by tool name rather than script path.
pub const NODE_TOOLS: &[&str] = &[
    "npm", "npx", "pnpm", "yarn", "vite", "next", "astro", "nuxt", "storybook", "tsx", "turbo",
    "playwright", "jest",
];

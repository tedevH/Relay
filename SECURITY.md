# Security Policy

## Reporting Vulnerabilities

Please report security issues privately by opening a GitHub security advisory for this repository.

Do not disclose vulnerabilities publicly until a fix or mitigation is available.

## Safety Model

Relay is local-first and shells out to tools already installed on the user's machine. It should not request API keys, modify `.env` files by itself, or push code except in explicit PR automation modes.

Security-sensitive changes should be reviewed carefully when they touch authentication, payments, migrations, secrets, or CI/CD configuration.

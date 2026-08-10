# Security policy

## Reporting a vulnerability

Please report security issues privately by opening a
[GitHub security advisory](https://github.com/AdvisorAGI/claude-workman/security/advisories/new)
rather than a public issue. We aim to acknowledge reports within a few days.

## Threat model — read before deploying

claude-workman gives whatever is connected to it **full mouse and keyboard control of a desktop
session**, plus the ability to read the screen and the accessibility tree. Anything a person
could do at that machine, an MCP client can do through this server.

Practical guidance:

- **Run it on a dedicated display, VM, or container** for unattended automation — not on the
  desktop where you keep signed-in banking, email, and password managers.
- **The screen is readable.** Screenshots and the accessibility tree can expose passwords,
  tokens, and private messages that happen to be visible.
- **The MCP transport is stdio.** Do not expose it over a network without your own
  authentication and transport security in front of it.
- **Use `show_cursor`** during interactive sessions so a human can see every action as it happens.
- **Prompt injection is a real risk**: content on screen (a web page, a document) can attempt to
  instruct the connected model. Keep a human in the loop for irreversible actions.

## Non-goals

claude-workman is not an anti-bot bypass tool. It does not solve CAPTCHAs, and contributions in
that direction are out of scope.

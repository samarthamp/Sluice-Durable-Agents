# Documentation

Start with the [project README](../README.md) for what Sluice is and how to run it.

| Document | Read it when you want to... |
|---|---|
| [architecture.md](architecture.md) | understand how it works: topology, data model, journal schema, the recovery algorithm, fencing, the oracle, the code layers |
| [design.md](design.md) | see the design decisions and the scenario analysis in two pages |
| [cli.md](cli.md) | look up a command, a flag, an environment variable, an HTTP endpoint or a tuning knob |
| [testing.md](testing.md) | run a subset of the tests, see what each test file proves, or chase down a failure |
| [verification.md](verification.md) | run `scripts/verify.sh`, or any of its ten steps by hand, and know what the right output is |
| [wsl-docker.md](wsl-docker.md) | set up WSL2 + Docker, or get past a Docker, Redis, port or line-ending problem |

## Diagrams

All three are plain SVG with an embedded draw.io model: they render the same in any viewer
(including dark-themed ones), and open for editing in [diagrams.net](https://app.diagrams.net).

| Diagram | Shows |
|---|---|
| [images/architecture.drawio.svg](images/architecture.drawio.svg) | the system topology, and how one alert flows through it |
| [images/recovery-protocol.drawio.svg](images/recovery-protocol.drawio.svg) | the poison step recovered: fork, barrier, LIFO compensation, one page |
| [images/code-architecture.drawio.svg](images/code-architecture.drawio.svg) | the package layers and where third-party libraries enter |

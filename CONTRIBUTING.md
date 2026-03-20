# Contributing to the Arbiter

Thanks for your interest in contributing to the Arbiter. This project is part of the [OpenSRM ecosystem](https://github.com/rsionnach/nthlayer-spec), and contributions of all kinds are welcome, whether that's bug reports, documentation improvements, feature proposals, or code.

## Getting Started

1. Fork the repository and clone your fork
2. Create a feature branch from `main`
3. Make your changes
4. Run any existing tests to make sure nothing is broken
5. Open a pull request with a clear description of what you changed and why

## Shared Conventions

The Arbiter follows the [OpenSRM specification](https://github.com/rsionnach/nthlayer-spec) for manifest formats, semantic conventions, and telemetry standards. If your contribution touches how the Arbiter reads manifests, emits telemetry, or interacts with other ecosystem components, please review the spec first to ensure alignment.

The Arbiter follows [Zero Framework Cognition](ZFC.md) as its core architectural principle. Transport (receiving data, routing it, persisting results) belongs in code. Judgment (evaluating quality, scoring output, deciding what's good or bad) belongs to the model. If you're unsure whether something is transport or judgment, check the ZFC document or open an issue to discuss.

## Reporting Issues

Use the GitHub issue templates for [bug reports](.github/ISSUE_TEMPLATE/bug_report.md) and [feature requests](.github/ISSUE_TEMPLATE/feature_request.md).

## Code of Conduct

Be kind, be constructive, and assume good intent. We're building tools to make systems more reliable, and that starts with how we treat each other.

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).

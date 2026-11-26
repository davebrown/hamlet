# Hamlet

![Hamlet logo](assets/hamlet-large.jpg)

This project is for using AI models to augment the effectiveness of SpamAssassin for the domain that I manage.

We'll run systematic evals against historical spam/ham corpus for the right prompt and model to hill climb to the best solution.

Here's what we're shooting for:

| Criterion | Target |
| --- | --- |
| Ham false-positive rate | One-sided 95% upper confidence bound ≤0.1%, not just the observed rate |
| Spam recall | ≥95%; 99% is the improvement goal |
| Critical ham | Zero observed misroutes on predefined critical/time-sensitive cases |
| Reliability | Zero observed system failures in the declared acceptance run |
| Cost | ≤$20/user/month at roughly 100 total emails/user/day |
| Latency | Compatible with Postfix/procmail delivery tolerance, including bursts and retries; exact acceptance measure still needs validation |

See [DEVELOPMENT.md](DEVELOPMENT.md) for setup and project details.

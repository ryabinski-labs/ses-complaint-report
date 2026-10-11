# Examples

The full, commented template lives at [`../.env.example`](../.env.example). Copy it
to `.env` and edit. These two files are ready-made starting points for common cases:

| File | Use when |
| --- | --- |
| [`demo.env`](demo.env) | You want to see the whole report on synthetic data with no AWS account. |
| [`minimal.env`](minimal.env) | You want to audit one real account's SES/CloudWatch posture without wiring up application stores. |

To use one, copy it over `.env`:

```sh
cp examples/demo.env .env
uv run sesreport
```

See the [configuration table](../README.md#configure) for every variable.

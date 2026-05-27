# Terraform

> **Status: placeholder.** No Terraform code lives here yet.
>
> Today's deployment paths:
> - **Single-server** — Bash / PowerShell installers in `scripts/`
>   (see [`STANDALONE_DEPLOYMENT.md`](../../STANDALONE_DEPLOYMENT.md)).
> - **Kubernetes** — Helm chart in [`infra/helm/`](../helm/).
>
> When multi-region or full IaC support lands (Phase 6 on the roadmap),
> the modules will live here. Sketch of the intended layout:
>
> ```
> infra/terraform/
>   modules/
>     vpc/              # per-region VPC + subnets + NAT
>     rds-postgres/     # managed Postgres with multi-AZ
>     ecs-execrelay/    # ECS Fargate task definitions per service
>     nats-cluster/     # NATS super-cluster across regions
>     observability/    # Managed Grafana, CloudWatch alarms
>   envs/
>     dev/
>     staging/
>     prod/
> ```
>
> Until then, this directory exists only as a future placeholder.

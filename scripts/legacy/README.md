# Legacy Controller Bring-Up

These files preserve the older controller workflow while the API-driven flow is being validated:

- `post_deploy_controllers.py`
- `add_controllers_to_vmanage.py`

That legacy sequence was:

1. `terraform apply`
2. `python3 ./scripts/legacy/post_deploy_controllers.py`
3. `python3 ./scripts/bootstrap_vmanage_cluster.py`
4. `python3 ./scripts/legacy/add_controllers_to_vmanage.py`

The active documented flow now uses:

1. `python3 ./scripts/stackit_disk_format.py`
2. `python3 ./scripts/stackit_cluster_certificate.py`

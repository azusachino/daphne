# Daphne k3s Deploy

These manifests follow the existing `harus-k3s/04-media/harus-bot` shape while using Daphne's `DAPHNE_*` environment variables.

Create the secret from a local env file, then apply the manifests:

```shell
cp deploy/k3s/secret-template.env deploy/k3s/secret.env
make secret ENV=deploy/k3s/secret.env NAME=daphne-secret NS=harus-media
kubectl apply -f deploy/k3s/rbac-configmap.yaml
kubectl apply -f deploy/k3s/pvc.yaml
kubectl apply -f deploy/k3s/deployment.yaml
```

Channel values can stay unchanged; only the variable names are Daphne-prefixed.

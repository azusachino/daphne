# Daphne k3s Deploy

These manifests follow the existing `harus-k3s/04-media/harus-bot` sidecar shape. Secrets stay in `daphne-secret`; runtime media-conversion settings and RBAC live in `daphne-config`.

Create the secret from a local env file, then apply the manifests:

```shell
cp deploy/k3s/secret-template.env deploy/k3s/secret.env
make secret ENV=deploy/k3s/secret.env NAME=daphne-secret NS=harus-media
kubectl apply -f deploy/k3s/configmap.yaml
kubectl apply -f deploy/k3s/tg-api-entrypoint.yaml
kubectl apply -f deploy/k3s/deployment.yaml
```

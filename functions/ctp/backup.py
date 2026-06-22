"""05-backup — S3 bucket, observe Cluster, BackupConfig, RBAC, BackupSchedule.

All resources here are gated on backup.enabled == "yes" (the caller in
main.py handles that gate). The BackupConfig/RBAC Objects are emitted
unconditionally inside that gate — provider-kubernetes Object resources stay
pending until UXP installs the BackupConfig CRD, then reconcile naturally.
"""

from crossplane.function import resource

from .prelude import stamp


def add_backup_resources(rsp, id_val, region, provider_config, bucket_name,
                        cluster_name, backup, uxp_deployed, config):
    # S3 Bucket — import-only. Delete is intentionally absent from
    # managementPolicies: deleting the XR removes this MR but leaves the AWS
    # bucket (and the backup data) intact.
    bucket = {
        "apiVersion": "s3.aws.m.upbound.io/v1beta1",
        "kind": "Bucket",
        "metadata": {
            "name": f"{id_val}-backup-bucket",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "backup-bucket",
                "crossplane.io/external-name": bucket_name
            }
        },
        "spec": {
            "managementPolicies": ["Observe", "Create", "Update", "LateInitialize"],
            "forProvider": {
                "region": region
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(bucket, config, aws_tags=True)
    resource.update(rsp.desired.resources["backup-bucket"], bucket)

    # Observe-only EKS Cluster — sources OIDC issuer URL + ARN for IRSA.
    # Falls back to id_val until the EKS XR reports the real cluster name.
    cluster_observe = {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "Cluster",
        "metadata": {
            "name": f"{id_val}-observe",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "eks-cluster-observe",
                "crossplane.io/external-name": cluster_name
            }
        },
        "spec": {
            "managementPolicies": ["Observe"],
            "forProvider": {
                "region": region
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    # Observe-only: tags would be ignored, so annotations only.
    stamp(cluster_observe, config)
    resource.update(rsp.desired.resources["eks-cluster-observe"], cluster_observe)

    # BackupConfig — the thanos objstore library requires config.endpoint;
    # without it the S3 client fails with "no s3 endpoint in config file".
    # credentials.source: InjectedIdentity uses the IRSA token mounted on
    # the upbound-controller-manager pod.
    backup_config = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": f"{id_val}-backup-config",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "backup-config"
            }
        },
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "admin.uxp.upbound.io/v1beta1",
                    "kind": "BackupConfig",
                    "metadata": {
                        "name": f"{id_val}-backup"
                    },
                    "spec": {
                        "objectStorage": {
                            "provider": "AWS",
                            "bucket": bucket_name,
                            "credentials": {
                                "source": "InjectedIdentity"
                            },
                            "config": {
                                "endpoint": "s3.amazonaws.com",
                                "region": region
                            }
                        }
                    }
                }
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(backup_config, config)
    resource.update(rsp.desired.resources["backup-config"], backup_config)

    # RBAC: the UXP Helm chart's default ClusterRole for
    # upbound-controller-manager does not grant access to
    # storeconfigs.secrets.crossplane.io. Backup export walks all Crossplane
    # resources including StoreConfigs, so without this extra ClusterRole the
    # backup fails at the export step with a 403.
    backup_rbac = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": f"{id_val}-backup-rbac",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "backup-rbac"
            }
        },
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "ClusterRole",
                    "metadata": {
                        "name": "upbound-backup-storeconfigs"
                    },
                    "rules": [
                        {
                            "apiGroups": ["secrets.crossplane.io"],
                            "resources": ["storeconfigs"],
                            "verbs": ["get", "list"]
                        }
                    ]
                }
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(backup_rbac, config)
    resource.update(rsp.desired.resources["backup-rbac"], backup_rbac)

    backup_rbac_binding = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": f"{id_val}-backup-rbac-binding",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "backup-rbac-binding"
            }
        },
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "ClusterRoleBinding",
                    "metadata": {
                        "name": "upbound-backup-storeconfigs"
                    },
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "ClusterRole",
                        "name": "upbound-backup-storeconfigs"
                    },
                    "subjects": [
                        {
                            "kind": "ServiceAccount",
                            "name": "upbound-controller-manager",
                            "namespace": "crossplane-system"
                        }
                    ]
                }
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(backup_rbac_binding, config)
    resource.update(rsp.desired.resources["backup-rbac-binding"], backup_rbac_binding)

    if backup.get("schedule"):
        backup_schedule = {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "metadata": {
                "name": f"{id_val}-backup-schedule",
                "namespace": "default",
                "annotations": {
                    "crossplane.io/composition-resource-name": "backup-schedule"
                }
            },
            "spec": {
                "forProvider": {
                    "manifest": {
                        "apiVersion": "admin.uxp.upbound.io/v1beta1",
                        "kind": "BackupSchedule",
                        "metadata": {
                            "name": f"{id_val}-schedule"
                        },
                        "spec": {
                            "schedule": backup["schedule"],
                            "configRef": {
                                "name": f"{id_val}-backup"
                            }
                        }
                    }
                },
                "providerConfigRef": {
                    "name": id_val,
                    "kind": "ProviderConfig"
                }
            }
        }
        stamp(backup_schedule, config)
        resource.update(rsp.desired.resources["backup-schedule"], backup_schedule)

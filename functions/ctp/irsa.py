"""06-irsa — OIDC Provider + IAM Role + Policy + ServiceAccount annotation +
controller restart + optional Restore-from-backup.

Gated by the caller on backup.enabled == "yes", OIDC URL present, and UXP
deployed — see compose() in main.py.
"""

import json

from crossplane.function import resource

from .prelude import extract_bucket_name, stamp


def add_irsa_resources(rsp, id_val, region, provider_config, oidc_host,
                      oidc_provider_arn, role_arn, bucket_name,
                      observed, install_from, account_id, config):
    oidc_provider = {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "OpenIDConnectProvider",
        "metadata": {
            "name": f"{id_val}-oidc",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "oidc-provider"
            }
        },
        "spec": {
            "forProvider": {
                "url": f"https://{oidc_host}",
                "clientIdList": ["sts.amazonaws.com"],
                "thumbprintList": ["9e99a48a9960b14926bb7f3b02e22da2b0ab7280"]
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(oidc_provider, config, aws_tags=True)
    resource.update(rsp.desired.resources["oidc-provider"], oidc_provider)

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Federated": oidc_provider_arn
                },
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{oidc_host}:sub": "system:serviceaccount:crossplane-system:upbound-controller-manager"
                    }
                }
            }
        ]
    }

    iam_role = {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "Role",
        "metadata": {
            "name": f"{id_val}-backup-irsa",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "backup-irsa-role"
            }
        },
        "spec": {
            "forProvider": {
                "assumeRolePolicy": json.dumps(trust_policy)
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(iam_role, config, aws_tags=True)
    resource.update(rsp.desired.resources["backup-irsa-role"], iam_role)

    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject"
                ],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*"
                ]
            }
        ]
    }

    iam_policy = {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "Policy",
        "metadata": {
            "name": f"{id_val}-backup-s3",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "backup-s3-policy"
            }
        },
        "spec": {
            "forProvider": {
                "policy": json.dumps(policy_doc)
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(iam_policy, config, aws_tags=True)
    resource.update(rsp.desired.resources["backup-s3-policy"], iam_policy)

    attachment = {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "RolePolicyAttachment",
        "metadata": {
            "name": f"{id_val}-backup-attach",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "backup-policy-attachment"
            }
        },
        "spec": {
            "forProvider": {
                "roleRef": {
                    "name": f"{id_val}-backup-irsa"
                },
                "policyArnSelector": {
                    "matchControllerRef": True
                }
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    # RolePolicyAttachment does not accept AWS tags — annotation only.
    stamp(attachment, config)
    resource.update(rsp.desired.resources["backup-policy-attachment"], attachment)

    # Patch the UXP ServiceAccount with the IRSA role ARN; the next pod
    # restart picks up the token-mount projection.
    sa_patch = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": f"{id_val}-backup-sa",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "backup-sa"
            }
        },
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {
                        "name": "upbound-controller-manager",
                        "namespace": "crossplane-system",
                        "annotations": {
                            "eks.amazonaws.com/role-arn": role_arn
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
    stamp(sa_patch, config)
    resource.update(rsp.desired.resources["backup-sa"], sa_patch)

    # Rolling restart of the controller deployment to pick up the new SA
    # projection. The kubectl.kubernetes.io/restartedAt value is a literal
    # string written once, which is enough to force a single rollout when the
    # Object is first applied.
    controller_restart = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {
            "name": f"{id_val}-controller-restart",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "controller-restart"
            }
        },
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "name": "upbound-controller-manager",
                        "namespace": "crossplane-system",
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": "{{ now }}"
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
    stamp(controller_restart, config)
    resource.update(rsp.desired.resources["controller-restart"], controller_restart)

    if install_from:
        source_bucket = extract_bucket_name(install_from.get("location", ""))
        restore_name = install_from.get("name", "")

        if source_bucket and restore_name:
            restore = {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {
                    "name": f"{id_val}-backup-restore",
                    "namespace": "default",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "backup-restore"
                    }
                },
                "spec": {
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "admin.uxp.upbound.io/v1beta1",
                            "kind": "Restore",
                            "metadata": {
                                "name": f"{id_val}-restore"
                            },
                            "spec": {
                                "backupRef": {
                                    "name": restore_name
                                },
                                "backupLocation": {
                                    "provider": "AWS",
                                    "bucket": source_bucket,
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
            stamp(restore, config)
            resource.update(rsp.desired.resources["backup-restore"], restore)

"""03b-lbcontroller — AWS Load Balancer Controller via EKS Pod Identity.

Installed when k8gb is enabled: k8gb's CoreDNS Service needs an NLB serving
UDP+TCP:53, which reliably requires this controller (the in-tree path defaults
to a Classic ELB that cannot do UDP / mixed-protocol).

Identity uses EKS Pod Identity (not IRSA): an IAM Role trusted by
pods.eks.amazonaws.com, attached to the published AWS Load Balancer Controller
policy, and bound to the kube-system/aws-load-balancer-controller
ServiceAccount via a PodIdentityAssociation. No OIDC provider, no SA
annotation, no controller restart. The eks-pod-identity-agent addon is already
installed by configuration-aws-eks.

The IAM Role/Policy render as soon as k8gb is enabled; the PodIdentityAssociation
and the Helm Release wait until the EKS cluster name is known (status.eks). The
release installs with replicaCount 0 until Pod Identity is wired, then scales
up — so `wait: true` never blocks on pods that cannot yet assume the role.
"""

import json

from crossplane.function import resource

from .prelude import stamp

# Published AWS Load Balancer Controller IAM policy (verbatim from the AWS docs,
# as shipped by configuration-aws-lb-controller). Grants the controller the
# ELB/EC2/WAF/Shield permissions it needs to reconcile NLBs/ALBs, target groups,
# listeners and security groups.
_LB_CONTROLLER_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["iam:CreateServiceLinkedRole"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "iam:AWSServiceName": "elasticloadbalancing.amazonaws.com"
                }
            },
        },
        {
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeAccountAttributes",
                "ec2:DescribeAddresses",
                "ec2:DescribeAvailabilityZones",
                "ec2:DescribeInternetGateways",
                "ec2:DescribeVpcs",
                "ec2:DescribeVpcPeeringConnections",
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeInstances",
                "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeTags",
                "ec2:GetCoipPoolUsage",
                "ec2:DescribeCoipPools",
                "elasticloadbalancing:DescribeLoadBalancers",
                "elasticloadbalancing:DescribeLoadBalancerAttributes",
                "elasticloadbalancing:DescribeListeners",
                "elasticloadbalancing:DescribeListenerCertificates",
                "elasticloadbalancing:DescribeSSLPolicies",
                "elasticloadbalancing:DescribeRules",
                "elasticloadbalancing:DescribeTargetGroups",
                "elasticloadbalancing:DescribeTargetGroupAttributes",
                "elasticloadbalancing:DescribeTargetHealth",
                "elasticloadbalancing:DescribeTags",
                "elasticloadbalancing:DescribeTrustStores",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "cognito-idp:DescribeUserPoolClient",
                "acm:ListCertificates",
                "acm:DescribeCertificate",
                "iam:ListServerCertificates",
                "iam:GetServerCertificate",
                "waf-regional:GetWebACL",
                "waf-regional:GetWebACLForResource",
                "waf-regional:AssociateWebACL",
                "waf-regional:DisassociateWebACL",
                "wafv2:GetWebACL",
                "wafv2:GetWebACLForResource",
                "wafv2:AssociateWebACL",
                "wafv2:DisassociateWebACL",
                "shield:GetSubscriptionState",
                "shield:DescribeProtection",
                "shield:CreateProtection",
                "shield:DeleteProtection",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:RevokeSecurityGroupIngress",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": ["ec2:CreateSecurityGroup"],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": ["ec2:CreateTags"],
            "Resource": "arn:aws:ec2:*:*:security-group/*",
            "Condition": {
                "StringEquals": {"ec2:CreateAction": "CreateSecurityGroup"},
                "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"},
            },
        },
        {
            "Effect": "Allow",
            "Action": ["ec2:CreateTags", "ec2:DeleteTags"],
            "Resource": "arn:aws:ec2:*:*:security-group/*",
            "Condition": {
                "Null": {
                    "aws:RequestTag/elbv2.k8s.aws/cluster": "true",
                    "aws:ResourceTag/elbv2.k8s.aws/cluster": "false",
                }
            },
        },
        {
            "Effect": "Allow",
            "Action": [
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:RevokeSecurityGroupIngress",
                "ec2:DeleteSecurityGroup",
            ],
            "Resource": "*",
            "Condition": {
                "Null": {"aws:ResourceTag/elbv2.k8s.aws/cluster": "false"}
            },
        },
        {
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:CreateLoadBalancer",
                "elasticloadbalancing:CreateTargetGroup",
            ],
            "Resource": "*",
            "Condition": {
                "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"}
            },
        },
        {
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:CreateListener",
                "elasticloadbalancing:DeleteListener",
                "elasticloadbalancing:CreateRule",
                "elasticloadbalancing:DeleteRule",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:AddTags",
                "elasticloadbalancing:RemoveTags",
            ],
            "Resource": [
                "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
                "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
                "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
            ],
            "Condition": {
                "Null": {
                    "aws:RequestTag/elbv2.k8s.aws/cluster": "true",
                    "aws:ResourceTag/elbv2.k8s.aws/cluster": "false",
                }
            },
        },
        {
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:AddTags",
                "elasticloadbalancing:RemoveTags",
            ],
            "Resource": [
                "arn:aws:elasticloadbalancing:*:*:listener/net/*/*/*",
                "arn:aws:elasticloadbalancing:*:*:listener/app/*/*/*",
                "arn:aws:elasticloadbalancing:*:*:listener-rule/net/*/*/*",
                "arn:aws:elasticloadbalancing:*:*:listener-rule/app/*/*/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:ModifyLoadBalancerAttributes",
                "elasticloadbalancing:SetIpAddressType",
                "elasticloadbalancing:SetSecurityGroups",
                "elasticloadbalancing:SetSubnets",
                "elasticloadbalancing:DeleteLoadBalancer",
                "elasticloadbalancing:ModifyTargetGroup",
                "elasticloadbalancing:ModifyTargetGroupAttributes",
                "elasticloadbalancing:DeleteTargetGroup",
            ],
            "Resource": "*",
            "Condition": {
                "Null": {"aws:ResourceTag/elbv2.k8s.aws/cluster": "false"}
            },
        },
        {
            "Effect": "Allow",
            "Action": ["elasticloadbalancing:AddTags"],
            "Resource": [
                "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
                "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
                "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*",
            ],
            "Condition": {
                "StringEquals": {
                    "elasticloadbalancing:CreateAction": [
                        "CreateTargetGroup",
                        "CreateLoadBalancer",
                    ]
                },
                "Null": {"aws:RequestTag/elbv2.k8s.aws/cluster": "false"},
            },
        },
        {
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:RegisterTargets",
                "elasticloadbalancing:DeregisterTargets",
            ],
            "Resource": "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:SetWebAcl",
                "elasticloadbalancing:ModifyListener",
                "elasticloadbalancing:AddListenerCertificates",
                "elasticloadbalancing:RemoveListenerCertificates",
                "elasticloadbalancing:ModifyRule",
            ],
            "Resource": "*",
        },
    ],
}


def add_lbcontroller_resources(rsp, id_val, provider_config, cluster_name,
                               account_id, region, lb_identity_ready,
                               lb_release_deployed, config):
    role_name = f"{id_val}-lb-controller"

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "pods.eks.amazonaws.com"},
                "Action": ["sts:AssumeRole", "sts:TagSession"],
            }
        ],
    }

    iam_role = {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "Role",
        "metadata": {
            "name": role_name,
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "lb-controller-role"
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
    resource.update(rsp.desired.resources["lb-controller-role"], iam_role)

    iam_policy = {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "Policy",
        "metadata": {
            "name": f"{id_val}-lb-controller",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "lb-controller-policy"
            }
        },
        "spec": {
            "forProvider": {
                "policy": json.dumps(_LB_CONTROLLER_POLICY)
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(iam_policy, config, aws_tags=True)
    resource.update(rsp.desired.resources["lb-controller-policy"], iam_policy)

    attachment = {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "RolePolicyAttachment",
        "metadata": {
            "name": f"{id_val}-lb-controller-attach",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "lb-controller-attach"
            }
        },
        "spec": {
            "forProvider": {
                "roleRef": {"name": role_name},
                # by-name (not matchControllerRef) so the backup policy, when
                # also present, is not ambiguously selected.
                "policyArnRef": {"name": f"{id_val}-lb-controller"}
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(attachment, config)
    resource.update(rsp.desired.resources["lb-controller-attach"], attachment)

    # PodIdentityAssociation + Helm Release need the EKS cluster name, which is
    # only known once the EKS XR surfaces status.eks.clusterArn.
    if not (cluster_name and account_id):
        return

    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    pod_identity = {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "PodIdentityAssociation",
        "metadata": {
            "name": f"{id_val}-lb-controller-pia",
            "namespace": "default",
            "annotations": {
                "crossplane.io/composition-resource-name": "lb-controller-pia"
            }
        },
        "spec": {
            "forProvider": {
                "region": region,
                "clusterName": cluster_name,
                "namespace": "kube-system",
                "serviceAccount": "aws-load-balancer-controller",
                "roleArn": role_arn
            },
            "providerConfigRef": {
                "name": provider_config,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(pod_identity, config)
    resource.update(rsp.desired.resources["lb-controller-pia"], pod_identity)

    release_annotations = {
        "crossplane.io/composition-resource-name": "lb-controller-release"
    }
    if lb_release_deployed:
        release_annotations["crossplane.io/ready"] = "True"

    release = {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "Release",
        "metadata": {
            "name": f"{id_val}-lb-controller",
            "namespace": "default",
            "annotations": release_annotations
        },
        "spec": {
            "forProvider": {
                "chart": {
                    "name": "aws-load-balancer-controller",
                    "repository": "https://aws.github.io/eks-charts",
                    # renovate: datasource=helm depName=aws-load-balancer-controller registryUrl=https://aws.github.io/eks-charts
                    "version": "1.8.3"
                },
                "namespace": "kube-system",
                "skipCreateNamespace": True,
                "wait": True,
                "values": {
                    "clusterName": cluster_name,
                    "region": region,
                    "serviceAccount": {
                        "name": "aws-load-balancer-controller"
                    },
                    # Install the chart resources immediately but keep the
                    # controller scaled to zero until Pod Identity is wired, so
                    # `wait: true` does not block on pods that cannot yet assume
                    # the role. Scale up once the association is Ready.
                    "replicaCount": 2 if lb_identity_ready else 0
                }
            },
            "providerConfigRef": {
                "name": id_val,
                "kind": "ProviderConfig"
            }
        }
    }
    stamp(release, config)
    resource.update(rsp.desired.resources["lb-controller-release"], release)

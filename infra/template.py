"""Generate the reviewable CloudFormation app template; no AWS calls."""
import json
from pathlib import Path


def ref(name):
    return {"Ref": name}


def attr(name, key):
    return {"Fn::GetAtt": [name, key]}


def sub(value):
    return {"Fn::Sub": value}


def build_template():
    resources = {}

    def resource(name, kind, properties, **extra):
        resources[name] = {"Type": "AWS::" + kind, "Properties": properties, **extra}

    # Reuse a VPC/IGW but create independent subnets, routes and security groups.
    # No existing route table, subnet, gateway or security group is modified.
    resource("PublicRoutes", "EC2::RouteTable", {"VpcId": ref("Vpc")})
    resource("InternetRoute", "EC2::Route", {
        "RouteTableId": ref("PublicRoutes"), "DestinationCidrBlock": "0.0.0.0/0",
        "GatewayId": ref("InternetGateway"),
    })
    resource("PrivateRoutes", "EC2::RouteTable", {"VpcId": ref("Vpc")})
    for zone in ("A", "B"):
        for kind, routes in (("Task", "PublicRoutes"), ("Origin", "PrivateRoutes")):
            name = f"{kind}Subnet{zone}"
            resource(name, "EC2::Subnet", {
                "VpcId": ref("Vpc"), "CidrBlock": ref(name + "Cidr"),
                "AvailabilityZone": ref(f"AvailabilityZone{zone}"), "MapPublicIpOnLaunch": False,
                "Tags": [{"Key": "Name", "Value": sub("${AWS::StackName}-" + name)}],
            })
            resource(name + "Routes", "EC2::SubnetRouteTableAssociation", {
                "SubnetId": ref(name), "RouteTableId": ref(routes),
            })
    resource("AlbSecurityGroup", "EC2::SecurityGroup", {
        "GroupDescription": "Only CloudFront may reach the internal application load balancer",
        "VpcId": ref("Vpc"),
        "SecurityGroupIngress": [{
            "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
            "SourcePrefixListId": ref("CloudFrontPrefixListId"),
        }],
        "SecurityGroupEgress": [{
            "IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, "CidrIp": ref("VpcCidr"),
        }],
    })
    resource("TaskSecurityGroup", "EC2::SecurityGroup", {
        "GroupDescription": "API accepts only ALB traffic and uses HTTPS for outbound requests",
        "VpcId": ref("Vpc"),
        "SecurityGroupIngress": [{
            "IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
            "SourceSecurityGroupId": ref("AlbSecurityGroup"),
        }],
        "SecurityGroupEgress": [{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0",
        }],
    })
    resource("LoadBalancer", "ElasticLoadBalancingV2::LoadBalancer", {
        "Type": "application", "Scheme": "internal",
        "SecurityGroups": [ref("AlbSecurityGroup")],
        "Subnets": [ref("OriginSubnetA"), ref("OriginSubnetB")],
        "LoadBalancerAttributes": [
            {"Key": "idle_timeout.timeout_seconds", "Value": "40"},
            {"Key": "routing.http.drop_invalid_header_fields.enabled", "Value": "true"},
        ],
    })
    resource("TargetGroup", "ElasticLoadBalancingV2::TargetGroup", {
        "VpcId": ref("Vpc"), "Port": 8000, "Protocol": "HTTP", "TargetType": "ip",
        "HealthCheckPath": "/healthz", "HealthCheckIntervalSeconds": 20,
        "HealthCheckTimeoutSeconds": 5, "HealthyThresholdCount": 2, "UnhealthyThresholdCount": 3,
        "Matcher": {"HttpCode": "200"},
        "TargetGroupAttributes": [{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}],
    })
    resource("Listener", "ElasticLoadBalancingV2::Listener", {
        "LoadBalancerArn": ref("LoadBalancer"), "Port": 80, "Protocol": "HTTP",
        "DefaultActions": [{"Type": "forward", "TargetGroupArn": ref("TargetGroup")}],
    })
    resource("ApiVpcOrigin", "CloudFront::VpcOrigin", {
        "VpcOriginEndpointConfig": {
            "Arn": ref("LoadBalancer"), "Name": sub("${AWS::StackName}-api"),
            "HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
            "OriginSSLProtocols": ["TLSv1.2"],
        },
    }, DependsOn=["Listener"])
    resource("Cluster", "ECS::Cluster", {
        "ClusterSettings": [{"Name": "containerInsights", "Value": "disabled"}],
    })
    resource("Logs", "Logs::LogGroup", {"RetentionInDays": 7})
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                       "Action": "sts:AssumeRole"}],
    }
    resource("ExecutionRole", "IAM::Role", {
        "AssumeRolePolicyDocument": trust,
        "ManagedPolicyArns": [sub("arn:${AWS::Partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")],
        "Policies": [{
            "PolicyName": "ReadRuntimeCredentials",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
                               "Resource": [ref("DeciderSecretArn"), ref("DemoSecretArn")]}],
            },
        }],
    })
    resource("TaskRole", "IAM::Role", {"AssumeRolePolicyDocument": trust})
    resource("TaskDefinition", "ECS::TaskDefinition", {
        "Cpu": "512", "Memory": "1024", "NetworkMode": "awsvpc",
        "RequiresCompatibilities": ["FARGATE"],
        "RuntimePlatform": {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"},
        "ExecutionRoleArn": attr("ExecutionRole", "Arn"), "TaskRoleArn": attr("TaskRole", "Arn"),
        "ContainerDefinitions": [{
            "Name": "api", "Image": ref("ImageUri"), "Essential": True,
            "ReadonlyRootFilesystem": True, "User": "10001",
            "PortMappings": [{"ContainerPort": 8000, "Protocol": "tcp"}],
            "LinuxParameters": {"InitProcessEnabled": True},
            "Environment": [
                {"Name": "DECIDER_ENDPOINT", "Value": ref("DeciderEndpoint")},
                {"Name": "DECIDER_TIMEOUT", "Value": "25"},
                {"Name": "MAX_CONCURRENCY", "Value": "2"},
                {"Name": "REQUESTS_PER_MINUTE", "Value": "90"},
            ],
            "Secrets": [
                {"Name": "DECIDER_API_KEY", "ValueFrom": ref("DeciderSecretArn")},
                {"Name": "DEMO_AUTH_CONFIG", "ValueFrom": ref("DemoSecretArn")},
            ],
            "LogConfiguration": {
                "LogDriver": "awslogs",
                "Options": {"awslogs-group": ref("Logs"), "awslogs-region": ref("AWS::Region"),
                            "awslogs-stream-prefix": "api"},
            },
        }],
    })
    resource("Service", "ECS::Service", {
        "Cluster": ref("Cluster"), "TaskDefinition": ref("TaskDefinition"),
        "LaunchType": "FARGATE", "PlatformVersion": "1.4.0", "DesiredCount": 1,
        "HealthCheckGracePeriodSeconds": 60,
        "DeploymentConfiguration": {
            "MaximumPercent": 200, "MinimumHealthyPercent": 100,
            "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
        },
        "NetworkConfiguration": {"AwsvpcConfiguration": {
            "AssignPublicIp": "ENABLED", "SecurityGroups": [ref("TaskSecurityGroup")],
            "Subnets": [ref("TaskSubnetA"), ref("TaskSubnetB")],
        }},
        "LoadBalancers": [{"ContainerName": "api", "ContainerPort": 8000,
                          "TargetGroupArn": ref("TargetGroup")}],
    }, DependsOn=["Listener", "InternetRoute", "TaskSubnetARoutes", "TaskSubnetBRoutes"])
    resource("FrontendBucket", "S3::Bucket", {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
        "BucketEncryption": {"ServerSideEncryptionConfiguration": [{
            "ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
        }]},
    }, DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    resource("OriginAccessControl", "CloudFront::OriginAccessControl", {
        "OriginAccessControlConfig": {
            "Name": sub("${AWS::StackName}-${AWS::Region}-assets"),
            "OriginAccessControlOriginType": "s3", "SigningBehavior": "always",
            "SigningProtocol": "sigv4",
        },
    })
    resource("StaticCachePolicy", "CloudFront::CachePolicy", {
        "CachePolicyConfig": {
            "Name": sub("${AWS::StackName}-${AWS::Region}-static"),
            "DefaultTTL": 0, "MinTTL": 0, "MaxTTL": 31536000,
            "ParametersInCacheKeyAndForwardedToOrigin": {
                "EnableAcceptEncodingGzip": True, "EnableAcceptEncodingBrotli": True,
                "CookiesConfig": {"CookieBehavior": "none"},
                "HeadersConfig": {"HeaderBehavior": "none"},
                "QueryStringsConfig": {"QueryStringBehavior": "none"},
            },
        },
    })
    resource("ApiCachePolicy", "CloudFront::CachePolicy", {
        "CachePolicyConfig": {
            "Name": sub("${AWS::StackName}-${AWS::Region}-api"),
            "DefaultTTL": 0, "MinTTL": 0, "MaxTTL": 0,
            "ParametersInCacheKeyAndForwardedToOrigin": {
                "EnableAcceptEncodingGzip": False,
                "CookiesConfig": {"CookieBehavior": "none"},
                "HeadersConfig": {"HeaderBehavior": "none"},
                "QueryStringsConfig": {"QueryStringBehavior": "none"},
            },
        },
    })
    resource("ApiOriginPolicy", "CloudFront::OriginRequestPolicy", {
        "OriginRequestPolicyConfig": {
            "Name": sub("${AWS::StackName}-${AWS::Region}-api"),
            "HeadersConfig": {"HeaderBehavior": "allExcept", "Headers": ["Host"]},
            "CookiesConfig": {"CookieBehavior": "whitelist", "Cookies": ["__Host-decider-session"]},
            "QueryStringsConfig": {"QueryStringBehavior": "none"},
        },
    })
    resource("SecurityHeaders", "CloudFront::ResponseHeadersPolicy", {
        "ResponseHeadersPolicyConfig": {
            "Name": sub("${AWS::StackName}-${AWS::Region}-security"),
            "SecurityHeadersConfig": {
                "ContentTypeOptions": {"Override": True},
                "FrameOptions": {"FrameOption": "DENY", "Override": True},
                "ReferrerPolicy": {"ReferrerPolicy": "no-referrer", "Override": True},
                "StrictTransportSecurity": {
                    "AccessControlMaxAgeSec": 31536000, "IncludeSubdomains": True, "Override": True,
                },
                "ContentSecurityPolicy": {
                    "Override": True,
                    "ContentSecurityPolicy": (
                        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
                    ),
                },
            },
        },
    })
    resource("SpaFunction", "CloudFront::Function", {
        "Name": sub("${AWS::StackName}-${AWS::Region}-spa"), "AutoPublish": True,
        "FunctionConfig": {"Comment": "SPA fallback applies only to static routes", "Runtime": "cloudfront-js-2.0"},
        "FunctionCode": (
            "function handler(event) { var r = event.request; "
            "if (r.uri.indexOf('/api') !== 0 && (r.uri.endsWith('/') || "
            "r.uri.split('/').pop().indexOf('.') === -1)) { r.uri = '/index.html'; } return r; }"
        ),
    })
    resource("Distribution", "CloudFront::Distribution", {
        "DistributionConfig": {
            "Enabled": True, "DefaultRootObject": "index.html",
            "Comment": "Decider Guardrails demonstration", "HttpVersion": "http2and3",
            "IPV6Enabled": True, "PriceClass": "PriceClass_100",
            "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
            "Origins": [
                {"Id": "frontend", "DomainName": attr("FrontendBucket", "RegionalDomainName"),
                 "OriginAccessControlId": ref("OriginAccessControl"),
                 "S3OriginConfig": {"OriginAccessIdentity": ""}},
                {"Id": "api", "DomainName": attr("LoadBalancer", "DNSName"),
                 "VpcOriginConfig": {"VpcOriginId": attr("ApiVpcOrigin", "Id"),
                                     "OriginReadTimeout": 30, "OriginKeepaliveTimeout": 5}},
            ],
            "DefaultCacheBehavior": {
                "TargetOriginId": "frontend", "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": ["GET", "HEAD"], "CachedMethods": ["GET", "HEAD"],
                "Compress": True, "CachePolicyId": ref("StaticCachePolicy"),
                "ResponseHeadersPolicyId": ref("SecurityHeaders"),
                "FunctionAssociations": [{"EventType": "viewer-request",
                                          "FunctionARN": attr("SpaFunction", "FunctionARN")}],
            },
            "CacheBehaviors": [{
                "PathPattern": "/api*", "TargetOriginId": "api",
                "ViewerProtocolPolicy": "https-only",
                "AllowedMethods": ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"],
                "CachedMethods": ["GET", "HEAD"], "Compress": True,
                "CachePolicyId": ref("ApiCachePolicy"), "OriginRequestPolicyId": ref("ApiOriginPolicy"),
                "ResponseHeadersPolicyId": ref("SecurityHeaders"),
            }],
        },
    })
    resource("BucketPolicy", "S3::BucketPolicy", {
        "Bucket": ref("FrontendBucket"),
        "PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "AllowOnlyThisCloudFrontDistribution", "Effect": "Allow",
                 "Principal": {"Service": "cloudfront.amazonaws.com"}, "Action": "s3:GetObject",
                 "Resource": sub("${FrontendBucket.Arn}/*"),
                 "Condition": {"StringEquals": {
                     "AWS:SourceArn": sub("arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/${Distribution}"),
                 }}},
                {"Sid": "DenyNonTls", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
                 "Resource": [attr("FrontendBucket", "Arn"), sub("${FrontendBucket.Arn}/*")],
                 "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
            ],
        },
    })
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Private S3/OAC frontend and ECS/Fargate API behind a CloudFront VPC origin",
        "Parameters": {
            "Vpc": {"Type": "AWS::EC2::VPC::Id"},
            "VpcCidr": {"Type": "String"},
            "InternetGateway": {"Type": "String"},
            "TaskSubnetACidr": {"Type": "String"},
            "TaskSubnetBCidr": {"Type": "String"},
            "OriginSubnetACidr": {"Type": "String"},
            "OriginSubnetBCidr": {"Type": "String"},
            "ImageUri": {"Type": "String"},
            "DeciderSecretArn": {"Type": "String"},
            "DemoSecretArn": {"Type": "String"},
            "DeciderEndpoint": {"Type": "String", "Default": "https://d1e9udi8st2p3g.cloudfront.net/v1/systemone"},
            "AvailabilityZoneA": {"Type": "AWS::EC2::AvailabilityZone::Name"},
            "AvailabilityZoneB": {"Type": "AWS::EC2::AvailabilityZone::Name"},
            "CloudFrontPrefixListId": {"Type": "String"},
        },
        "Resources": resources,
        "Outputs": {
            "WebsiteUrl": {"Value": sub("https://${Distribution.DomainName}")},
            "DistributionId": {"Value": ref("Distribution")},
            "FrontendBucket": {"Value": ref("FrontendBucket")},
            "Cluster": {"Value": ref("Cluster")},
            "Service": {"Value": attr("Service", "Name")},
            "LogGroup": {"Value": ref("Logs")},
            "LoadBalancerDns": {"Value": attr("LoadBalancer", "DNSName")},
            "VpcOriginId": {"Value": attr("ApiVpcOrigin", "Id")},
        },
    }


if __name__ == "__main__":
    path = Path(__file__).with_name("app.json")
    path.write_text(json.dumps(build_template(), indent=2) + "\n")
    print(path)

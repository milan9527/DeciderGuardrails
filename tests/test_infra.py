from infra.template import build_template


def test_static_bucket_is_private_and_scoped_to_cloudfront_oac():
    resources = build_template()["Resources"]
    bucket = resources["FrontendBucket"]["Properties"]
    assert all(bucket["PublicAccessBlockConfiguration"].values())
    assert "WebsiteConfiguration" not in bucket
    policy = resources["BucketPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]
    assert policy["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert "AWS:SourceArn" in policy["Condition"]["StringEquals"]
    assert resources["OriginAccessControl"]["Properties"]["OriginAccessControlConfig"]["SigningBehavior"] == "always"


def test_private_api_disables_caching_and_forwards_session_cookie():
    resources = build_template()["Resources"]
    assert resources["LoadBalancer"]["Properties"]["Scheme"] == "internal"
    distribution = resources["Distribution"]["Properties"]["DistributionConfig"]
    assert "VpcOriginConfig" in distribution["Origins"][1]
    assert "CustomErrorResponses" not in distribution  # Do not convert API errors to HTML 200s.
    assert resources["ApiCachePolicy"]["Properties"]["CachePolicyConfig"]["MaxTTL"] == 0
    headers = resources["ApiOriginPolicy"]["Properties"]["OriginRequestPolicyConfig"]["HeadersConfig"]
    assert headers == {"HeaderBehavior": "allExcept", "Headers": ["Host"]}
    cookies = resources["ApiOriginPolicy"]["Properties"]["OriginRequestPolicyConfig"]["CookiesConfig"]
    assert cookies == {"CookieBehavior": "whitelist", "Cookies": ["__Host-decider-session"]}


def test_task_secrets_are_injected_and_task_has_no_aws_permissions():
    resources = build_template()["Resources"]
    task = resources["TaskDefinition"]["Properties"]
    container = task["ContainerDefinitions"][0]
    assert task["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
    assert container["ReadonlyRootFilesystem"]
    assert {s["Name"] for s in container["Secrets"]} == {"DECIDER_API_KEY", "DEMO_AUTH_CONFIG"}
    assert "Policies" not in resources["TaskRole"]["Properties"]
    assert not any("KEY" in e["Name"] or "TOKEN" in e["Name"] for e in container["Environment"])


def test_existing_vpc_and_gateway_are_reused_without_modifying_existing_network():
    template = build_template()
    types = [resource["Type"] for resource in template["Resources"].values()]
    assert "AWS::EC2::VPC" not in types
    assert "AWS::EC2::InternetGateway" not in types
    assert types.count("AWS::EC2::Subnet") == 4
    assert template["Parameters"]["Vpc"]["Type"] == "AWS::EC2::VPC::Id"

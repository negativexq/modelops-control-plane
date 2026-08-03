from app.control_plane.models import DeploymentStatus

# PENDING -> DEPLOYING -> CANARY_RUNNING -> EVALUATING -> (PROMOTING -> PROMOTED |
#                                                          ROLLING_BACK -> ROLLED_BACK |
#                                                          INCONCLUSIVE)
# INCONCLUSIVE requires a human decision: promote or roll back manually.
# Any in-flight state can fail outright.
ALLOWED_TRANSITIONS: dict[DeploymentStatus, frozenset[DeploymentStatus]] = {
    DeploymentStatus.PENDING: frozenset({DeploymentStatus.DEPLOYING, DeploymentStatus.FAILED}),
    DeploymentStatus.DEPLOYING: frozenset(
        {DeploymentStatus.CANARY_RUNNING, DeploymentStatus.FAILED}
    ),
    DeploymentStatus.CANARY_RUNNING: frozenset(
        {DeploymentStatus.EVALUATING, DeploymentStatus.FAILED}
    ),
    DeploymentStatus.EVALUATING: frozenset(
        {
            DeploymentStatus.PROMOTING,
            DeploymentStatus.ROLLING_BACK,
            DeploymentStatus.INCONCLUSIVE,
            DeploymentStatus.FAILED,
        }
    ),
    DeploymentStatus.PROMOTING: frozenset({DeploymentStatus.PROMOTED, DeploymentStatus.FAILED}),
    DeploymentStatus.ROLLING_BACK: frozenset(
        {DeploymentStatus.ROLLED_BACK, DeploymentStatus.FAILED}
    ),
    DeploymentStatus.INCONCLUSIVE: frozenset(
        {DeploymentStatus.PROMOTING, DeploymentStatus.ROLLING_BACK}
    ),
    DeploymentStatus.PROMOTED: frozenset(),
    DeploymentStatus.ROLLED_BACK: frozenset(),
    DeploymentStatus.FAILED: frozenset(),
}


class InvalidTransitionError(Exception):
    def __init__(self, current: DeploymentStatus, target: DeploymentStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition deployment from {current.value} to {target.value}")


def validate_transition(current: DeploymentStatus, target: DeploymentStatus) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(current, target)

class JobServiceError(Exception):
    pass


class JobNotFound(JobServiceError):
    pass


class InvalidJobRequest(JobServiceError):
    pass


class IdempotencyConflict(JobServiceError):
    pass


class JobStateConflict(JobServiceError):
    pass

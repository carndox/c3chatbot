CREATE TABLE FBPosts (
    id INT IDENTITY(1,1) PRIMARY KEY,
    post_id VARCHAR(50) UNIQUE NOT NULL,
    post_url NVARCHAR(500) NOT NULL,
    post_time DATETIME,
    text NVARCHAR(MAX),
    summary NVARCHAR(MAX),
    attachments NVARCHAR(MAX)
);
GO

namespace Revend.EventDesktop;

partial class Form1
{
    private System.ComponentModel.IContainer components = null;
    private Panel topBar;
    private Label titleLabel;
    private Label statusLabel;
    private Button reloadButton;

    protected override void Dispose(bool disposing)
    {
        if (disposing && (components != null))
        {
            components.Dispose();
        }
        base.Dispose(disposing);
    }

    private void InitializeComponent()
    {
        topBar = new Panel();
        titleLabel = new Label();
        statusLabel = new Label();
        reloadButton = new Button();
        topBar.SuspendLayout();
        SuspendLayout();
        
        topBar.BackColor = Color.FromArgb(18, 57, 46);
        topBar.Controls.Add(reloadButton);
        topBar.Controls.Add(statusLabel);
        topBar.Controls.Add(titleLabel);
        topBar.Dock = DockStyle.Top;
        topBar.Location = new Point(0, 0);
        topBar.Name = "topBar";
        topBar.Size = new Size(1280, 56);
        topBar.TabIndex = 0;

        titleLabel.AutoSize = true;
        titleLabel.Font = new Font("Segoe UI Semibold", 11F, FontStyle.Bold);
        titleLabel.ForeColor = Color.White;
        titleLabel.Location = new Point(16, 17);
        titleLabel.Name = "titleLabel";
        titleLabel.Size = new Size(221, 20);
        titleLabel.TabIndex = 0;
        titleLabel.Text = "Revend Event Service - Desktop";

        statusLabel.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        statusLabel.AutoSize = true;
        statusLabel.Font = new Font("Segoe UI", 9F);
        statusLabel.ForeColor = Color.FromArgb(240, 200, 75);
        statusLabel.Location = new Point(980, 20);
        statusLabel.Name = "statusLabel";
        statusLabel.Size = new Size(136, 15);
        statusLabel.TabIndex = 1;
        statusLabel.Text = "Connecting to service...";

        reloadButton.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        reloadButton.BackColor = Color.FromArgb(20, 149, 111);
        reloadButton.FlatAppearance.BorderSize = 0;
        reloadButton.FlatStyle = FlatStyle.Flat;
        reloadButton.Font = new Font("Segoe UI", 9F, FontStyle.Bold);
        reloadButton.ForeColor = Color.White;
        reloadButton.Location = new Point(1140, 13);
        reloadButton.Name = "reloadButton";
        reloadButton.Size = new Size(125, 30);
        reloadButton.TabIndex = 2;
        reloadButton.Text = "Reload";
        reloadButton.UseVisualStyleBackColor = false;
        reloadButton.Click += reloadButton_Click;

        AutoScaleMode = AutoScaleMode.Font;
        BackColor = Color.White;
        ClientSize = new Size(1280, 820);
        Controls.Add(topBar);
        MinimumSize = new Size(1000, 700);
        Name = "Form1";
        StartPosition = FormStartPosition.CenterScreen;
        Text = "Revend Event Desktop";
        WindowState = FormWindowState.Maximized;
        Load += Form1_Load;
        topBar.ResumeLayout(false);
        topBar.PerformLayout();
        ResumeLayout(false);
    }
}

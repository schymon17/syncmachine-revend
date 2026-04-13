namespace Revend.EventDesktop;

using System.Diagnostics;
using System.Net.Http;
using Microsoft.Web.WebView2.WinForms;

public partial class Form1 : Form
{
    private readonly string _serviceBaseUrl;
    private readonly string _uiUrl;
    private readonly string _healthUrl;
    private readonly HttpClient _httpClient = new();
    private readonly WebView2 _webView;
    private Process? _serviceProcess;

    public Form1(string serviceBaseUrl)
    {
        _serviceBaseUrl = serviceBaseUrl.TrimEnd('/');
        _uiUrl = $"{_serviceBaseUrl}/ui";
        _healthUrl = $"{_serviceBaseUrl}/health";

        InitializeComponent();

        _webView = new WebView2
        {
            Dock = DockStyle.Fill,
            Name = "webView"
        };
        Controls.Add(_webView);
        _webView.SendToBack();
    }

    private async void Form1_Load(object? sender, EventArgs e)
    {
        var ready = await EnsureServiceRunningAsync();
        if (!ready)
        {
            statusLabel.Text = "Service unavailable";
            MessageBox.Show(
                "Nie udalo sie uruchomic backendu automatycznie. Uruchom Revend.EventService i sproboj ponownie.",
                "Backend unavailable",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
            return;
        }

        statusLabel.Text = "Service online";
        await _webView.EnsureCoreWebView2Async();
        _webView.Source = new Uri(_uiUrl);
    }

    private async Task<bool> EnsureServiceRunningAsync()
    {
        if (await IsServiceHealthyAsync())
        {
            return true;
        }

        TryStartBundledServiceProcess();

        for (var i = 0; i < 20; i++)
        {
            await Task.Delay(500);
            if (await IsServiceHealthyAsync())
            {
                return true;
            }
        }

        return false;
    }

    private async Task<bool> IsServiceHealthyAsync()
    {
        try
        {
            using var response = await _httpClient.GetAsync(_healthUrl);
            return response.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    private void TryStartBundledServiceProcess()
    {
        try
        {
            if (_serviceProcess is { HasExited: false })
            {
                return;
            }

            var baseDir = AppDomain.CurrentDomain.BaseDirectory;
            var serviceExe = Path.Combine(baseDir, "Revend.EventService.exe");
            if (!File.Exists(serviceExe))
            {
                return;
            }

            _serviceProcess = Process.Start(new ProcessStartInfo
            {
                FileName = serviceExe,
                Arguments = $"--urls {_serviceBaseUrl}",
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = baseDir
            });
        }
        catch
        {
            // no-op, user can run backend manually
        }
    }

    private void reloadButton_Click(object? sender, EventArgs e)
    {
        _webView.Reload();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        try
        {
            if (_serviceProcess is { HasExited: false })
            {
                _serviceProcess.Kill(true);
            }
        }
        catch
        {
            // ignore shutdown errors
        }

        _httpClient.Dispose();
        base.OnFormClosed(e);
    }
}
